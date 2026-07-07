"""REST API consommée par le frontend oto.ninja (page de gestion de compte).

Endpoints (ce fichier — gestion compte, providers,
tools, admin, WhatsApp) :
- `GET    /api/me`                            → infos user + rôle + statut keys
- `GET    /api/settings/api-keys/{provider}`  → état/clé (tout connecteur byo_user à secret simple)
- `POST   /api/settings/api-keys/{provider}`  → pose le credential : `api_key`→`{key}` ; `basic_auth`→`{email,password}`
- `DELETE /api/settings/api-keys/{provider}`  → efface
- `GET    /api/me/tools` + `POST/DELETE /api/me/tools/{name}` → toggle tools per-user
- `GET    /api/admin/*`                       → admin (users, platform-keys, grants, tokens)

Endpoints datastore / Google OAuth / API tokens : voir `api_routes_datastore.py`.
Endpoints SIRENE stock : voir `api_routes_sirene.py`.
Endpoints organisation (`/api/me/orgs`, `/api/orgs/*`, `/api/admin/orgs/*`,
`/api/admin/namespace-grants*`) : voir `api_routes_orgs.py` — projection REST du
palier org (mêmes fonctions de service que les meta-tools MCP `oto_admin_*org*`).

Auth : Bearer JWT Logto **ou** API token long-lived (préfixe `oto_`), vérifié
via `_authenticate`. Le frontend obtient le token Logto via `@logto/vue`. La
CLI utilise un API token issu sur `/account` (stocké en SOPS sous `OTO_API_KEY`).

CORS : limité aux origines oto.ninja (+ localhost en dev).
"""
from __future__ import annotations

import os
from typing import Iterable

import asyncio
import base64
import html as _html
import json
import logging
import re
import time

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                                  Response, StreamingResponse)

from . import access, api_routes_atlassian, api_routes_connectors, api_routes_contact, api_routes_datastore, api_routes_folk, api_routes_memento, api_routes_sirene, billing, connector_activation, connectors, db, group_store, memento_oauth, org_store, tool_registry
from .capabilities import _rest_adapter as _cap_rest_adapter
from .capabilities import registry as _cap_registry
from . import auth_hooks
from .tool_visibility import (
    PROTECTED_TOOLS, is_default_hidden, is_testable, namespace_of)

logger = logging.getLogger(__name__)


def _allowed_origins() -> list[str]:
    raw = os.environ.get("OTO_MCP_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://oto.cx",                   # domaine marketing canonique (cutover ADR 0040)
        "https://www.oto.cx",
        "https://manage.oto.cx",            # oto-dashboard PROD (cutover ADR 0040)
        "https://oto.ninja",                # preprod/canari + redirections
        "https://www.oto.ninja",
        "https://app.oto.ninja",
        "https://otomata.tech",             # formulaire de contact vitrine
        "https://www.otomata.tech",
        "http://localhost:5173",
        "http://localhost:4173",
        "http://localhost:5182",
        "http://localhost:5184",
        "http://localhost:5192",            # oto-dashboard dev (ADR 0007)
        "https://dashboard.otoninja.dev",   # oto-dashboard via Caddy local
        "https://dashboard.oto.ninja",      # oto-dashboard prod
    ]



def _cors_headers(origin: str | None) -> dict[str, str]:
    if origin and origin in _allowed_origins():
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Oto-Org, X-Oto-Group, X-Oto-View-As",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
    return {}


def _maybe_view_as(real_sub: str, apply_view_as: bool) -> str:
    """Applique le « voir en tant que » (axe user, REST lecture seule) : si un sub
    de consultation est posé pour la requête (par ViewAsMiddleware, qui a DÉJÀ validé
    opérateur + cible + GET), renvoie ce sub cible ; sinon le sub réel. `apply_view_as`
    False = chemin du middleware lui-même (qui doit voir le sub RÉEL pour gater)."""
    if not apply_view_as:
        return real_sub
    from . import session_org
    target = session_org.current_view_user()
    return target if (target and target != real_sub) else real_sub


async def _authenticate(
    request: Request,
    verifier: JWTVerifier,
    *,
    allow_query_token: bool = False,
    apply_view_as: bool = True,
) -> tuple[str | None, JSONResponse | None]:
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif allow_query_token:
        # Fallback pour SSE via EventSource (qui n'autorise pas les headers).
        token = request.query_params.get("token")
    if not token:
        return None, _json_error(request, 401, "missing_bearer")

    # API token long-lived (CLI) : préfixe `oto_` → lookup hash en DB.
    # Pas de upsert_user ici : la FK CASCADE garantit que si la row user a
    # été supprimée, le token a été supprimé avec.
    if token.startswith("oto_"):
        # DB HORS de la loop (threadpool) : un blip DB ne doit jamais geler le
        # serveur mono-loop entier (vécu 2026-07-02, py-spy : getconn wait ici).
        sub = await run_in_threadpool(db.verify_api_token, token)
        if not sub:
            return None, _json_error(request, 401, "invalid_api_token")
        return _maybe_view_as(sub, apply_view_as), None

    # Sinon, JWT Logto.
    access_token = await verifier.verify_token(token)
    if not access_token or not getattr(access_token, "claims", None):
        return None, _json_error(request, 401, "invalid_token")
    sub = access_token.claims.get("sub")
    if not sub:
        return None, _json_error(request, 401, "missing_sub")
    # Bascule de tenant (B1) : pendant la fenêtre, canonicaliser le sub AVANT l'upsert
    # (un vieux token de l'ancien tenant en drain → compte migré, sinon il re-créerait
    # le compte supprimé). Gaté env → no-op hors bascule.
    if os.environ.get("OTO_MCP_TENANT_MIGRATION_ISS"):
        sub = await run_in_threadpool(db.resolve_sub, sub)
    # upsert_user = DB à CHAQUE requête REST → threadpool (jamais dans la loop).
    await run_in_threadpool(
        lambda: db.upsert_user(sub, email=access_token.claims.get("email"),
                               name=access_token.claims.get("name"),
                               iss=access_token.claims.get("iss")))
    return _maybe_view_as(sub, apply_view_as), None


def _json_error(request: Request, status: int, code: str,
                detail: str | None = None) -> JSONResponse:
    payload = {"error": code}
    if detail:
        payload["detail"] = detail
    return JSONResponse(
        payload,
        status_code=status,
        headers=_cors_headers(request.headers.get("origin")),
    )


def _json(request: Request, payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status, headers=_cors_headers(request.headers.get("origin"))
    )


def _upload_page_html(label: str | None) -> str:
    """Page d'upload autoportée d'un lien signé (#105, fallback humain). `label` None
    = lien invalide/expiré (message, sans formulaire). Le POST du fichier se fait vers
    la MÊME URL (multipart `file`), en fetch, avec accusé/erreur affiché."""
    if label is None:
        body = ('<h1>Lien d’upload invalide ou expiré</h1>'
                '<p>Demande à l’assistant de régénérer un lien.</p>')
    else:
        safe = _html.escape(label)
        body = (
            f'<h1>Déposer un fichier</h1><p class="tgt">Destination : <b>{safe}</b></p>'
            '<form id="f"><input type="file" name="file" id="file" required>'
            '<button type="submit">Envoyer</button></form>'
            '<p id="msg" class="msg"></p>'
            '<script>'
            'const f=document.getElementById("f"),m=document.getElementById("msg");'
            'f.addEventListener("submit",async e=>{e.preventDefault();'
            'const fi=document.getElementById("file");'
            'if(!fi.files.length){return}'
            'const fd=new FormData();fd.append("file",fi.files[0]);'
            'm.textContent="Envoi…";m.className="msg";'
            'try{const r=await fetch(location.href,{method:"POST",body:fd});'
            'const j=await r.json().catch(()=>({}));'
            'if(r.ok){f.style.display="none";m.textContent="✓ Reçu. Tu peux fermer cette page.";m.className="msg ok"}'
            'else{m.textContent="Échec : "+(j.error||r.status)+(j.detail?" — "+j.detail:"");m.className="msg err"}'
            '}catch(err){m.textContent="Erreur réseau.";m.className="msg err"}});'
            '</script>')
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Upload — oto</title><style>'
        ':root{color-scheme:light dark}'
        'body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.2rem}'
        'h1{font-size:1.5rem;margin:0 0 .6rem}.tgt{color:#666}'
        'form{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin:1.4rem 0}'
        'button{padding:.55rem 1.1rem;border:0;border-radius:.5rem;background:#4f46e5;color:#fff;font:inherit;cursor:pointer}'
        'button:hover{background:#4338ca}.msg{min-height:1.5rem}.ok{color:#16a34a}.err{color:#dc2626}'
        '</style></head><body>' + body + '</body></html>')


def _project_org_context_error(request: Request, sub: str, pid: int):
    """Gate de CONTEXTE d'org (ADR 0023) des routes projet par-id : le projet doit être
    visible dans l'org de CONSULTATION (`access.current_org`), pas seulement accessible
    à l'acteur via une AUTRE de ses orgs (fuite cross-org — cf. l'incident projet). Le
    pendant REST du gate de la capacité `oto_project`. Renvoie une 404 non-disclosante
    si hors contexte, sinon None. Les routes d'ÉCRITURE gardent en plus leur check de
    permission `can_access(write)`."""
    from . import ownership
    if ownership.visible_in_org(sub, access.current_org(sub), "project", str(pid)):
        return None
    return _json_error(request, 404, "unknown_project")


# ── View-as (ADR 0023) : consultation d'une org dans le dashboard ───────────
def _parse_view_org(request: Request) -> int | None:
    """Org de consultation (header `X-Oto-Org`). None = pas de header ; 0 = perso ;
    >0 = id d'org. Header mal formé → None (repli maison, jamais d'erreur dure)."""
    raw = request.headers.get("x-oto-org")
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("", "0", "perso", "personal"):
        return 0
    try:
        n = int(v)
        return n if n > 0 else 0
    except ValueError:
        return None


def _parse_view_group(request: Request) -> int | None:
    """Équipe de consultation (header `X-Oto-Group`). None = pas de header / niveau
    org ; >0 = id de groupe. Pas de sentinelle perso (l'absence = niveau org)."""
    raw = request.headers.get("x-oto-group")
    if raw is None:
        return None
    try:
        n = int(raw.strip())
        return n if n > 0 else None
    except ValueError:
        return None


def _parse_view_user(request: Request) -> str | None:
    """User de consultation (« voir en tant que », header `X-Oto-View-As` = sub cible).
    None = pas de header. Validé (opérateur + cible existe + GET) dans le middleware."""
    raw = request.headers.get("x-oto-view-as")
    if raw is None:
        return None
    return raw.strip() or None


class ViewAsMiddleware:
    """Middleware ASGI **brut** (pas BaseHTTPMiddleware, qui bufferiserait le
    streaming `/mcp`) : n'intervient QUE sur `/api/*` portant `X-Oto-Org`, sinon
    pass-through total. Pose l'org de consultation (contextvar `session_org`) lue
    par le seam `access.current_org` → toute la résolution REST (autz + handlers +
    visibilité) scope la consultation, **sans** persister ni muter l'identité.

    Anti-IDOR : l'appartenance est validée ici (org>0) ; on ne fait JAMAIS confiance
    à l'en-tête. Sans header, ou non authentifié → la route suit son cours normal."""

    def __init__(self, app, verifier: JWTVerifier):
        self.app = app
        self._verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        request = Request(scope, receive)  # headers/query seulement → ne consomme pas le body
        view_org = _parse_view_org(request)
        view_group = _parse_view_group(request)
        view_user = _parse_view_user(request)
        if view_org is None and view_group is None and view_user is None:
            return await self.app(scope, receive, send)
        # sub RÉEL (apply_view_as=False) : sert à gater, jamais à appliquer la consultation.
        sub, err = await _authenticate(request, self._verifier, apply_view_as=False)
        if err:  # non authentifié → la route rendra son 401 ; pas de view-as
            return await self.app(scope, receive, send)
        from . import access, db, group_store, org_store, roles, session_org
        if view_user:  # « voir en tant que » : opérateur plateforme + cible existe + LECTURE SEULE
            if not await run_in_threadpool(access.is_platform_operator, sub):
                return await _json_error(request, 403, "forbidden")(scope, receive, send)
            if request.method != "GET":  # consultation = lecture seule, jamais d'écriture en son nom
                return await _json_error(request, 403, "view_as_read_only")(scope, receive, send)
            if view_user == sub or await run_in_threadpool(db.get_user, view_user) is None:
                view_user = None  # cible = soi ou inconnue → pas de consultation (no-op)
        if view_group:  # équipe consultée → valide la lecture + DÉRIVE son org parente (invariant)
            g = await run_in_threadpool(group_store.get_group, view_group)
            if g is None or not await run_in_threadpool(roles.can_read_group, sub, view_group):
                return await _json_error(request, 403, "forbidden")(scope, receive, send)
            view_org = g["org_id"]
        elif view_org:  # org>0 (0=perso = profil global, pas de check)
            # Membership RÉELLE (colonne DB, PAS l'escalade super_admin) : un membre
            # consulte son org normalement (lecture + écriture selon son rôle).
            real_role = await run_in_threadpool(org_store.get_org_role, view_org, sub)
            if real_role is not None:
                pass  # membre réel — comportement inchangé (writes gatés par le rôle)
            elif await run_in_threadpool(access.is_platform_operator, sub):
                # Opérateur plateforme NON-membre : inspection d'une org tierce en
                # LECTURE SEULE (même patron que le view-as user) — jamais d'écriture,
                # même pour un super_admin (mode inspection ≠ escalade d'admin).
                if request.method != "GET":
                    return await _json_error(request, 403, "view_as_read_only")(scope, receive, send)
            else:
                return await _json_error(request, 403, "forbidden")(scope, receive, send)
        usr_token = session_org.set_view_user(view_user) if view_user is not None else None
        org_token = session_org.set_view_org(view_org) if view_org is not None else None
        grp_token = session_org.set_view_group(view_group) if view_group is not None else None
        try:
            return await self.app(scope, receive, send)
        finally:
            if grp_token is not None:
                session_org.reset_view_group(grp_token)
            if org_token is not None:
                session_org.reset_view_org(org_token)
            if usr_token is not None:
                session_org.reset_view_user(usr_token)


# --- Journalisation des appels REST dans le flux unifié (ADR 0017, kind='rest') ---
# La face MCP est tracée par otomata-calllog ; la face REST ne l'était PAS (3/4 de
# la plateforme invisibles au monitoring). Ce middleware comble le trou : une ligne
# tool_calls(kind='rest') par requête /api/*, dérivée du même substrat.

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_REST_LOG_TASKS: set = set()  # garde les refs des tâches fire-and-forget (anti-GC)


def _claimed_sub(request: Request) -> str | None:
    """Sub revendiqué par le bearer JWT, **NON vérifié** — attribution de log
    uniquement (jamais d'autz ; la route, elle, vérifie pour de vrai). Best-effort :
    token API opaque (`oto_…`) ou JWT malformé → None (ligne anonyme)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    parts = auth[7:].strip().split(".")
    if len(parts) != 3:  # pas un JWT → token opaque, pas d'attribution
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
        sub = claims.get("sub")
        return sub if isinstance(sub, str) else None
    except Exception:
        return None


def _normalize_route(path: str) -> str:
    """Réduit la cardinalité pour l'agrégation : segments d'id (numériques / UUID)
    → `:id`. `/api/orgs/7/audit-log` → `/api/orgs/:id/audit-log`."""
    return "/".join(
        ":id" if (seg.isdigit() or _UUID_RE.match(seg)) else seg
        for seg in path.split("/")
    )


async def _emit_rest_event(row: dict) -> None:
    """Écrit l'événement hors event-loop (to_thread → insert sync non bloquant).
    Best-effort : une panne de log n'a jamais d'effet sur la requête servie."""
    try:
        await asyncio.to_thread(db.insert_tool_call, row)
    except Exception:  # noqa: BLE001 — le monitoring ne casse jamais le service
        logger.debug("rest call-log emit failed", exc_info=True)


class RestCallLogger:
    """Middleware ASGI **brut** : journalise chaque requête `/api/*` comme événement
    `kind='rest'` du flux unifié (ADR 0017). Pass-through total hors `/api/*` (ne
    touche JAMAIS le streaming `/mcp`) et sur les préflights `OPTIONS` (bruit CORS).
    `tool` = `MÉTHODE /route-normalisée` ; `ok` = 2xx/3xx ; les ≥400 portent le code
    dans `error`. Écriture en tâche de fond → zéro latence ajoutée, jamais bloquant."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        if method == "OPTIONS":
            return await self.app(scope, receive, send)
        status = {"code": 0}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status["code"] = message.get("status", 0)
            await send(message)

        request = Request(scope, receive)  # headers/query only → ne consomme pas le body
        sub = _claimed_sub(request)
        org = _parse_view_org(request)  # org de consultation revendiquée (header), best-effort
        started = time.monotonic()
        try:
            await self.app(scope, receive, _send)
        finally:
            code = status["code"]
            row = {
                "kind": "rest",
                "tool": f"{method} {_normalize_route(scope.get('path', ''))}",
                "sub": sub,
                "org_id": org,
                "ok": 200 <= code < 400,
                "error": (f"HTTP {code}" if code >= 400 else None),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            task = asyncio.create_task(_emit_rest_event(row))
            _REST_LOG_TASKS.add(task)
            task.add_done_callback(_REST_LOG_TASKS.discard)


def make_routes(verifier: JWTVerifier, mcp_instance=None) -> Iterable:
    from starlette.routing import Route

    async def options_handler(request: Request) -> Response:
        return Response(status_code=204, headers=_cors_headers(request.headers.get("origin")))

    async def mcp_catalog(request: Request) -> JSONResponse:
        """Liste publique des tools MCP exposés — alimente l'autodoc oto.ninja.

        Pas d'auth : la doc des tools (nom, description, schémas) est de toute
        façon découvrable via tools/list du protocole MCP. CORS large pour
        permettre fetch côté oto.ninja.
        """
        if mcp_instance is None:
            return _json(request, {"tools": []})
        try:
            tools = await mcp_instance.list_tools(run_middleware=False)
        except Exception as e:
            return _json_error(request, 500, f"list_tools_failed:{e}")
        payload = []
        # (Le filtre « bridges remote per-namespace » a été retiré — ADR 0034 B4 :
        # le namespace `bridge` est générique, aucun nom client n'atteint l'autodoc.)
        for t in tools:
            # Tool object exposes name, description, parameters (input schema),
            # output_schema. Some attributes may be None depending on the type.
            payload.append({
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": getattr(t, "parameters", None),
                "output_schema": getattr(t, "output_schema", None),
            })
        return _json(request, {"tools": payload, "count": len(payload)})

    async def connectors_catalog(request: Request) -> JSONResponse:
        """Catalogue des connecteurs (registre source unique), auth optionnelle.

        Cran d'activation (ADR 0010) filtré EN AMONT de la visibilité : un
        connecteur non activé (master global OFF sans override d'org ON) n'apparaît
        pas dans la vue PRODUIT (anonyme + non-admin). L'**admin voit tout le
        registre** — sa vue de gouvernance sert justement à activer/désactiver.
        Ensuite, visibilité : anonyme → self-serve seuls (les `platform_granted`,
        dont les bridges client-sensibles ADR 0003, sont deny-by-default comme sur
        la face MCP) ; non-admin authentifié → + ceux dont un namespace est entitled
        pour le sub (override d'org appliqué via son org active).
        """
        cat = connectors.public_catalog()
        if not request.headers.get("authorization"):
            exposed = connector_activation.exposed_connectors(None)
            cat = [c for c in cat if c["name"] in exposed]
            cat = [c for c in cat if c["availability"] != "platform_granted"]
            return _json(request, {"connectors": cat})
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_platform_operator(sub):
            # Visibilité par l'activation (master × override d'org). Un connecteur à
            # clé plateforme réservé (ex. scaleway) est tenu hors des orgs non
            # autorisées par son activation (master OFF + override org ON), plus par
            # un grant de namespace (retiré, ADR 0031).
            # Org de CONTEXTE (seam ADR 0023 : consultation X-Oto-Org > maison) —
            # le catalogue suit l'org consultée au dashboard, comme status_for.
            exposed = connector_activation.exposed_connectors(access.current_org(sub))
            cat = [c for c in cat if c["name"] in exposed]
        return _json(request, {"connectors": cat})

    async def doctrines_library_public(request: Request) -> JSONResponse:
        """Catalogue PUBLIC des doctrines (bibliothèque/marketplace) — pas d'auth.

        Alimente le site vitrine oto.ninja. Deny-by-default : `visibility='public'`
        UNIQUEMENT (jamais 'unlisted' ni les brouillons d'org). Filtres gros grain
        en query params (`q`/`category`/`author`) ; le filtrage fin reste client.
        Route écrite à la main car l'adaptateur REST des capacités authentifie
        toujours (l'anonyme ne peut pas y passer).
        """
        q = request.query_params
        try:
            limit = min(int(q.get("limit", "100")), 200)
        except ValueError:
            limit = 100
        items = org_store.list_library(
            query=q.get("q"), category=q.get("category"),
            author_kind=q.get("author"), include_unlisted=False, limit=limit)
        return _json(request, {"doctrines": items})

    async def doctrines_library_public_get(request: Request) -> JSONResponse:
        """Une doctrine PUBLIQUE complète (markdown) par slug — vitrine, pas d'auth.
        Public-only : une entrée 'unlisted' n'est jamais servie ici."""
        entry = org_store.get_library_entry(
            slug=request.path_params["slug"], include_unlisted=False)
        if not entry:
            return _json_error(request, 404, "unknown_entry")
        return _json(request, entry)

    async def invite_preview(request: Request) -> JSONResponse:
        """Aperçu PUBLIC d'une invitation (pas d'auth — le token est le secret).
        Alimente la page d'accueil « vous êtes invité·e » avant la création de
        compte : email visé + inviteur, pour accompagner l'onboarding."""
        p = org_store.preview_invitation(request.path_params.get("token", ""))
        if not p:
            return _json_error(request, 404, "invalid_or_expired")
        return _json(request, p)

    async def invite_preview_by_code(request: Request) -> JSONResponse:
        """Aperçu PUBLIC d'une invitation par code court (/invitation/<c>/<code>)."""
        p = org_store.preview_invitation_by_code(request.path_params.get("code", ""))
        if not p:
            return _json_error(request, 404, "invalid_or_expired")
        return _json(request, p)

    async def referral_preview(request: Request) -> JSONResponse:
        """Aperçu PUBLIC d'un lien referral réutilisable (/invitation/<carrier>)."""
        p = org_store.preview_referral(request.path_params.get("carrier", ""))
        if not p:
            return _json_error(request, 404, "invalid_or_expired")
        return _json(request, p)

    async def me(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        user = db.get_user(sub) or {}
        status = access.status_for(sub)
        # `active_org` = org EFFECTIVE (ADR 0023) : via `current_org` elle reflète
        # la consultation view-as (header X-Oto-Org) si posée, sinon la maison. Le
        # front scope ses vues là-dessus. `home_org` (ci-dessous) = le défaut brut.
        active_org = access.current_org(sub)
        active_org_name = None
        active_org_logo_url = None
        org_role = None
        active_org_require_mfa = False
        if active_org is not None:
            o = org_store.get_org(active_org)
            active_org_name = o["name"] if o else None
            # Logo EFFECTIF (upload > dérivé logo.dev du domaine déclaré).
            active_org_logo_url = org_store.effective_logo_url(o) if o else None
            org_role = org_store.get_org_role(active_org, sub)
            # MFA obligatoire de l'org (2ᵉ facteur imposé au login des membres,
            # enforcé par Logto via l'org miroir — cf. mfa_mirror).
            active_org_require_mfa = org_store.get_org_mfa(active_org)["require_mfa"]
        # Consultation d'une org tierce EN LECTURE SEULE par un opérateur plateforme :
        # org active posée (par X-Oto-Org) mais aucun rôle réel dans cette org. Le front
        # affiche un bandeau + traite l'écran en lecture (le backend rejette déjà toute
        # mutation — GET-only au middleware). Un membre a toujours un rôle → False.
        active_org_readonly = (
            active_org is not None and org_role is None
            and access.is_platform_operator(sub)
        )
        # Org MAISON (défaut persistant, colonne) — exposée distinctement pour que
        # le front affiche « ton défaut » et l'action « définir comme maison ».
        home_org = org_store.get_active_org(sub)
        home_org_name = None
        if home_org is not None and home_org != active_org:
            ho = org_store.get_org(home_org)
            home_org_name = ho["name"] if ho else None
        elif home_org is not None:
            home_org_name = active_org_name
        # Sous-palier groupe (ADR 0012) : équipe EFFECTIVE (consultation ?? maison,
        # ADR 0023) + rôle effectif (escalade). `home_group` = défaut persistant.
        active_group = access.current_group(sub)
        active_group_name = None
        group_role = None
        if active_group is not None:
            from . import roles
            g = group_store.get_group(active_group)
            active_group_name = g["name"] if g else None
            group_role = roles.effective_group_role(sub, active_group)
        home_group = group_store.get_active_group(sub)
        home_group_name = None
        if home_group is not None and home_group != active_group:
            hg = group_store.get_group(home_group)
            home_group_name = hg["name"] if hg else None
        elif home_group is not None:
            home_group_name = active_group_name
        return _json(request, {
            "sub": sub,
            "email": user.get("email"),
            "name": user.get("name"),
            "avatar_url": user.get("avatar_url"),
            # Préférence de langue de l'UI dashboard ('en'|'fr'), NULL = non définie
            # (le front retombe sur la langue du navigateur). Écrite via PUT /api/me/locale.
            "locale": user.get("locale"),
            "role": status["role"],
            "active_org": active_org,
            "active_org_name": active_org_name,
            "active_org_logo_url": active_org_logo_url,
            "org_role": org_role,
            "active_org_readonly": active_org_readonly,
            "active_org_require_mfa": active_org_require_mfa,
            "home_org": home_org,
            "home_org_name": home_org_name,
            "active_group": active_group,
            "active_group_name": active_group_name,
            "group_role": group_role,
            "home_group": home_group,
            "home_group_name": home_group_name,
            "access": {
                "status": user.get("access_status"),
                "invites_left": user.get("invite_quota", 0),
                "invited_by": user.get("invited_by"),
            },
            # Feature flags par-déploiement (dark launch) : le dashboard dérive sa
            # nav de l'effet backend (ex. billing masqué en prod tant que le PSP
            # n'est pas live) — une seule source, pas de flag front dupliqué.
            "features": {"billing": billing.is_enabled()},
            # crunchbase = connecteur `personal_session` standard → exposé dans
            # `providers` (comme brevo), plus de bloc dédié (ADR 0026).
            # Fédération MCP (otomata#16) : statut du compte memento fédéré du user
            # — alimente l'auto-prompt « connecter memento » du dashboard.
            "memento": memento_oauth.status_for(sub),
            "providers": status["providers"],
        })

    # Saisie de credential per-user, GÉNÉRIQUE (modèle multi-champs, ADR 0011) :
    # tout connecteur `byo_user` qui déclare un schéma de saisie (`secret_fields` :
    # api_key 1 champ, basic_auth 2 champs, silae 3 champs…). Le formulaire, la
    # validation et le packing dérivent du schéma — zéro branche par connecteur.
    # cookie/oauth ont des flux dédiés (crunchbase/brevo via Live View Browserbase,
    # google/memento via OAuth) → `secret_fields` vide → exclus ici.
    # --- Avatar user + logo d'org (Object Storage) -------------------------
    # Upload multipart → ne passe PAS par la couche capacité (ADR 0009 = corps
    # JSON pydantic). URL publique persistée en clair (pas un secret).

    async def _read_upload(request: Request):
        """Parse un multipart, renvoie (data: bytes, err: JSONResponse|None)."""
        try:
            form = await request.form()
        except Exception:
            return None, _json_error(request, 400, "invalid_multipart")
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return None, _json_error(request, 400, "missing_file")
        return await upload.read(), None

    async def avatar_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        data, err = await _read_upload(request)
        if err:
            return err
        from . import media_store
        try:
            url = media_store.upload_image("avatars", sub, data, "")
        except media_store.MediaError as e:
            return _json_error(request, e.status, e.code)
        old = (db.get_user(sub) or {}).get("avatar_url")
        db.set_avatar_url(sub, url)
        if old and old != url:
            media_store.delete_by_url(old)
        return _json(request, {"ok": True, "avatar_url": url})

    async def avatar_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        old = (db.get_user(sub) or {}).get("avatar_url")
        db.set_avatar_url(sub, None)
        if old:
            from . import media_store
            media_store.delete_by_url(old)
        return _json(request, {"ok": True})

    # --- Fichiers bruts d'un projet — carte « Autre document » (ADR 0032 §3) ---
    # Upload multipart (PDF/HTML…) → hors couche capacité (corps binaire, pas JSON).
    # Blob DURABLE+privé en Object Storage ; accès par presigned à la lecture.

    def _signed(row: dict) -> dict:
        from . import media_store
        key = row.pop("s3_key", None)
        try:
            row["download_url"] = media_store.presign_get(key) if key else None
        except media_store.MediaError:
            row["download_url"] = None
        return row

    async def project_files_list(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        pid = int(request.path_params["project_id"])
        if not db.get_project_by_id(pid):
            return _json_error(request, 404, "unknown_project")
        if (e := _project_org_context_error(request, sub, pid)):
            return e
        return _json(request, {"files": [_signed(r) for r in db.list_project_files(pid)]})

    async def project_files_upload(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        from . import ownership, media_store
        pid = int(request.path_params["project_id"])
        if not db.get_project_by_id(pid):
            return _json_error(request, 404, "unknown_project")
        if (e := _project_org_context_error(request, sub, pid)):
            return e
        if not ownership.can_access(sub, "project", str(pid), "write"):
            return _json_error(request, 403, "forbidden")
        try:
            form = await request.form()
        except Exception:
            return _json_error(request, 400, "invalid_multipart")
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return _json_error(request, 400, "missing_file")
        data = await upload.read()
        filename = getattr(upload, "filename", None) or "file"
        content_type = getattr(upload, "content_type", None) or "application/octet-stream"
        title = (str(form.get("title") or "")).strip() or None
        description = (str(form.get("description") or "")).strip() or None
        try:
            key = media_store.upload_object("project-files", str(pid), data, content_type, filename)
        except media_store.MediaError as e:
            return _json_error(request, e.status, e.code)
        row = db.add_project_file(pid, key, filename, mime=content_type,
                                  size_bytes=len(data), title=title,
                                  description=description, created_by=sub)
        db.log_project_activity(pid, sub, "project.file_add", title or filename)
        return _json(request, {"ok": True, "file": _signed(row)})

    async def project_file_delete(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        from . import ownership, media_store
        pid = int(request.path_params["project_id"])
        file_id = int(request.path_params["file_id"])
        existing = db.get_project_file(file_id)
        if not existing or existing["project_id"] != pid:
            return _json_error(request, 404, "unknown_file")
        if (e := _project_org_context_error(request, sub, pid)):
            return e
        if not ownership.can_access(sub, "project", str(pid), "write"):
            return _json_error(request, 403, "forbidden")
        db.delete_project_file(file_id)
        media_store.delete_by_key(existing["s3_key"])
        db.log_project_activity(pid, sub, "project.file_delete",
                                existing.get("title") or existing.get("filename"))
        return _json(request, {"ok": True})

    async def project_file_public(request: Request) -> JSONResponse:
        """Bascule le partage public d'un fichier (ADR 0032 §3, B4b) : ACL S3
        public-read ↔ private, URL publique permanente persistée."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        from . import ownership, media_store
        pid = int(request.path_params["project_id"])
        file_id = int(request.path_params["file_id"])
        existing = db.get_project_file(file_id)
        if not existing or existing["project_id"] != pid:
            return _json_error(request, 404, "unknown_file")
        if (e := _project_org_context_error(request, sub, pid)):
            return e
        if not ownership.can_access(sub, "project", str(pid), "write"):
            return _json_error(request, 403, "forbidden")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        make_public = bool(isinstance(body, dict) and body.get("public"))
        try:
            public_url = media_store.make_public(existing["s3_key"]) if make_public else None
        except media_store.MediaError as e:
            return _json_error(request, e.status, e.code)
        if not make_public:
            media_store.make_private(existing["s3_key"])
        row = db.set_project_file_public(file_id, make_public, public_url)
        db.log_project_activity(pid, sub, "project.file_public",
                                f"{existing.get('title') or existing.get('filename')}:{make_public}")
        return _json(request, {"ok": True, "file": _signed(row)})

    async def _do_signed_upload(request: Request, payload: dict, data: bytes,
                                ct: str | None) -> JSONResponse:
        """Cœur commun des réceptions d'upload signé : autz réappliquée → borne de
        taille → consommation à usage unique → matérialisation. DB sync → threadpool."""
        from . import upload_tokens
        sub, target = payload["sub"], payload["target"]
        try:
            await run_in_threadpool(upload_tokens.check_target_access, sub, target)
        except upload_tokens.UploadError as e:
            return _json_error(request, e.status, e.code)
        if not data:
            return _json_error(request, 400, "empty_body")
        if len(data) > upload_tokens.max_bytes():
            return _json_error(request, 413, "content_too_large")
        # Consommer AVANT de matérialiser (anti-rejeu / double-écriture).
        if not await run_in_threadpool(db.consume_upload_token, payload["jti"]):
            return _json_error(request, 409, "token_already_used")
        try:
            result = await run_in_threadpool(upload_tokens.materialize, sub, target, data, ct)
        except upload_tokens.UploadError as e:
            return _json_error(request, e.status, e.code)
        return _json(request, result)

    async def upload_receive(request: Request) -> JSONResponse:
        """Réception d'un upload signé (issue #105) : PAS de JWT — le jeton signé DANS
        l'URL fait foi (scellé sub/org/cible, TTL, usage unique). Deux voies :
        **PUT** = un agent avec shell y pousse le corps brut (`curl --data-binary`) ;
        **POST** multipart `file` = le formulaire humain (fallback claude.ai). On
        matérialise en RÉAPPLIQUANT l'autz de la cible. Accusé léger, jamais le body."""
        from . import upload_tokens
        payload = upload_tokens.verify(request.path_params.get("token", ""))
        if payload is None:
            return _json_error(request, 401, "invalid_or_expired_token")
        if request.method == "POST":
            try:
                form = await request.form()
            except Exception:
                return _json_error(request, 400, "invalid_multipart")
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                return _json_error(request, 400, "missing_file")
            data = await upload.read()
            ct = getattr(upload, "content_type", None)
        else:  # PUT — corps brut
            data = await request.body()
            ct = request.headers.get("content-type")
        return await _do_signed_upload(request, payload, data, ct)

    async def upload_form(request: Request) -> Response:
        """Page HTML d'upload d'un lien signé (GET) — **fallback humain** quand l'agent
        n'a pas de shell (claude.ai lui transmet ce lien). Le jeton n'est PAS consommé
        au GET (seulement au POST du fichier). Autoportée (aucun asset externe)."""
        from . import upload_tokens
        payload = upload_tokens.verify(request.path_params.get("token", ""))
        if payload is None:
            return HTMLResponse(_upload_page_html(None), status_code=401)
        return HTMLResponse(_upload_page_html(upload_tokens.target_label(payload["target"])))

    async def public_doc(request: Request) -> JSONResponse:
        """Lecture publique d'un doc partagé par token (gap #4a) — PAS d'auth,
        lecture seule. Le dashboard rend le markdown sur sa route publique /p/d/<token>."""
        token = request.path_params.get("token", "")
        doc = db.get_doc_by_public_token(token) if token else None
        if not doc:
            return _json_error(request, 404, "not_found")
        return _json(request, {"title": doc["title"], "body_md": doc["body_md"],
                               "updated_at": doc.get("updated_at")})

    async def public_doc_view(request: Request) -> Response:
        """Page de partage PUBLIQUE d'un doc — route `/p/d/<token>`, **server-rendered**
        pour être lisible par un agent (WebFetch sans JS) autant que par un navigateur.
        Négocie sur `Accept` : `application/json` → JSON, `text/markdown` → markdown brut,
        sinon HTML autoporté (`public_doc_page`). PAS d'auth, lecture seule."""
        from . import public_doc_page
        token = request.path_params.get("token", "")
        doc = db.get_doc_by_public_token(token) if token else None
        accept = request.headers.get("accept", "").lower()
        wants_json = "application/json" in accept
        if not doc:
            if wants_json:
                return _json_error(request, 404, "not_found")
            return HTMLResponse(public_doc_page.render_missing(), status_code=404)
        title, body_md = doc["title"], doc.get("body_md") or ""
        if wants_json:
            return _json(request, {"title": title, "body_md": body_md,
                                   "updated_at": doc.get("updated_at")})
        if "text/markdown" in accept:
            md = f"# {title}\n\n{body_md}" if title else body_md
            return PlainTextResponse(md, media_type="text/markdown; charset=utf-8",
                                     headers={"Cache-Control": "public, max-age=300"})
        html_page = public_doc_page.render(title=title, body_md=body_md,
                                           updated_at=doc.get("updated_at"))
        return HTMLResponse(html_page, headers={"Cache-Control": "public, max-age=300"})

    def _org_logo_gate(request: Request, sub: str):
        """Renvoie (org_id, err). 400 id invalide, 404 org inconnue, 403 non-admin."""
        from . import roles
        try:
            org_id = int(request.path_params["id"])
        except (ValueError, KeyError):
            return None, _json_error(request, 400, "invalid_id")
        if not org_store.get_org(org_id):
            return None, _json_error(request, 404, "unknown_org")
        if not roles.is_org_admin(sub, org_id):
            return None, _json_error(request, 403, "forbidden")
        return org_id, None

    async def org_logo_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, err = _org_logo_gate(request, sub)
        if err:
            return err
        data, err = await _read_upload(request)
        if err:
            return err
        from . import media_store
        try:
            url = media_store.upload_image("org-logos", str(org_id), data, "")
        except media_store.MediaError as e:
            return _json_error(request, e.status, e.code)
        old = (org_store.get_org(org_id) or {}).get("logo_url")
        org_store.set_org_logo(org_id, url)
        if old and old != url:
            media_store.delete_by_url(old)
        return _json(request, {"ok": True, "logo_url": url})

    async def org_logo_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        org_id, err = _org_logo_gate(request, sub)
        if err:
            return err
        old = (org_store.get_org(org_id) or {}).get("logo_url")
        org_store.set_org_logo(org_id, None)
        if old:
            from . import media_store
            media_store.delete_by_url(old)
        return _json(request, {"ok": True})

    # Saisie de credential per-user, GÉNÉRIQUE (dérivée du registre, pas une liste
    # hardcodée) : tout connecteur `byo_user` dont le secret est un "secret simple"
    # — `api_key` (la clé) ou `basic_auth` (base64("email:password"), ex. planity).
    # cookie/oauth ont des flows dédiés (crunchbase / google / memento) → exclus ici.
    _SETTABLE_KINDS = {"api_key", "basic_auth"}

    def _credentialable(provider: str):
        c = connectors.connector_for_provider(provider)
        if c is None or not connectors.is_byo_user(provider) or not c.secret_fields:
            return None
        return c

    async def api_key_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        c = _credentialable(provider)
        if c is None:
            return _json_error(request, 404, "unknown_provider")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        # Chaque champ `required` doit être non vide ; un champ facultatif
        # (connecteur « ET/OU » type slack) peut être omis, mais il faut au moins
        # un champ posé au total. Le packing (raw/base64/json) est encapsulé dans
        # credentials_store.pack_secret.
        from . import credentials_store
        fields: dict[str, str] = {}
        for f in c.secret_fields:
            val = credentials_store.clean_field_value(f, body.get(f.name))
            if not val:
                if f.required:
                    return _json_error(request, 400, "missing_credentials")
                continue
            fields[f.name] = val
        if not fields:
            return _json_error(request, 400, "missing_credentials")
        db.upsert_user(sub)
        account = (body.get("account") or "").strip()
        # Scope MEMBRE (ADR 0033) : la clé est posée DANS l'org de contexte (org
        # consultée au dashboard via X-Oto-Org, sinon maison) — plus de credential
        # per-user org-agnostique. Poser en consultant movinmotion = scoper movinmotion.
        org_id = access.current_org(sub)
        if org_id is None:
            return _json_error(request, 400, "no_org_context")
        eid = credentials_store.member_id(org_id, sub)
        # Multi-compte (« 2 Zoho ») : cohérence des comptes de CE membre pour le
        # connecteur. '' (mono legacy) et comptes nommés ne doivent pas coexister
        # (sinon la désambiguïsation à la résolution voit un '' impossible à désigner).
        if c.auth_multi_account:
            existing = [r["account"] for r in
                        credentials_store.list_accounts(credentials_store.MEMBER, eid, provider)]
            if account:
                # Backfill lazy : au 1er compte NOMMÉ, la ligne '' migre vers un label
                # (« principal », suffixé si déjà pris ou = à l'account posé).
                if "" in existing:
                    taken, target, i = set(existing) | {account}, "principal", 2
                    while target in taken:
                        target, i = f"principal-{i}", i + 1
                    credentials_store.rename_account(
                        credentials_store.MEMBER, eid, provider, "", target)
            elif any(a for a in existing):
                return _json_error(
                    request, 409, "account_required",
                    "Ce connecteur a déjà des comptes nommés — précise `account`.")
        secret = credentials_store.pack_secret(provider, fields)
        # Version d'API portée par le credential (v1/v2 « selon la BYO ») : pour
        # unipile, choisir v2 range {api_version, dsn} dans le meta (lu par
        # resolve_credential → config → unipile_client / hosted-auth). Absence de
        # meta = défaut v1 ; un re-set en v1 remet meta à {} (EXCLUDED.meta).
        meta = None
        if provider == "unipile" and str(body.get("api_version") or "").lower() in ("v2", "2"):
            meta = {"api_version": "v2", "dsn": "api.unipile.com"}
        credentials_store.set_credential(
            credentials_store.MEMBER, eid, provider, secret, set_by=sub,
            account=account, meta=meta)
        return _json(request, {"ok": True, "provider": provider, "org_id": org_id,
                               "account": account,
                               "api_version": (meta or {}).get("api_version", "v1")})

    async def api_key_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        # Effacer est générique : tout connecteur byo_user (clé multi-champs OU
        # session navigateur sans champ, ex. brevo/crunchbase). On ne dépend PAS de
        # `secret_fields` comme GET/SAVE — sinon la déconnexion d'une session
        # Browserbase 404 (route `/api/settings/crunchbase` retirée par ADR 0026).
        c = connectors.connector_for_provider(provider)
        if c is None or not connectors.is_byo_user(provider):
            return _json_error(request, 404, "unknown_provider")
        from . import credentials_store
        org_id = access.current_org(sub)
        if org_id is None:
            return _json_error(request, 400, "no_org_context")
        # Multi-compte : `?account=` cible un compte précis ('' = mono legacy).
        account = (request.query_params.get("account") or "").strip()
        credentials_store.clear_credential(
            credentials_store.MEMBER, credentials_store.member_id(org_id, sub), provider,
            account=account)
        return _json(request, {"ok": True, "provider": provider, "account": account})

    # --- Connexion par session navigateur (brevo, crunchbase) — la VOIE PRODUIT :
    # le bouton « Connecter » du dashboard ouvre une Live View Browserbase en iframe,
    # l'utilisateur se logue, puis « finalize » vérifie + persiste le Context. Même
    # corps de logique que les tools MCP `<name>_connect_start/_status` (seam partagé
    # `browser_session`). `start` est BLOQUANT (HTTP Browserbase) → `to_thread`.
    async def session_start(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        from . import browser_session
        name = request.path_params["name"]
        if not browser_session.is_session_connector(name):
            return _json_error(request, 404, "not_a_session_connector")
        try:
            out = await asyncio.to_thread(browser_session.start, sub, name)
        except browser_session.SessionError as e:
            return _json_error(request, 503, "browserbase_unavailable", str(e))
        return _json(request, out)

    async def session_finalize(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        from . import browser_session
        name = request.path_params["name"]
        if not browser_session.is_session_connector(name):
            return _json_error(request, 404, "not_a_session_connector")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        context_id = (body or {}).get("context_id")
        session_id = (body or {}).get("session_id")
        if not context_id or not session_id:
            return _json_error(request, 400, "missing_params")
        try:
            connected = await browser_session.finalize(sub, name, context_id, session_id)
        except browser_session.SessionError as e:
            return _json_error(request, 502, "session_verify_failed", str(e))
        return _json(request, {"connected": connected})

    async def api_key_get(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        c = _credentialable(provider)
        if c is None:
            return _json_error(request, 404, "unknown_provider")
        from . import credentials_store
        org_id = access.current_org(sub)
        secret = (credentials_store.get_credential(
                      credentials_store.MEMBER,
                      credentials_store.member_id(org_id, sub), provider)
                  if org_id is not None else None)
        if not secret:
            return _json_error(request, 404, "not_configured")
        # GÉNÉRIQUE : on dépack et on ne renvoie que les champs `reveal` (l'api_key,
        # pour copier) ou non-`secret` (l'email). Jamais un mot de passe / secret.
        fields = credentials_store.unpack_secret(provider, secret)
        out: dict = {"provider": provider, "configured": True}
        for f in c.secret_fields:
            if f.reveal or not f.secret:
                out[f.name] = fields.get(f.name)
        return _json(request, out)

    async def admin_platform_keys_list(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_super_admin(sub):
            return _json_error(request, 403, "forbidden")
        # On ne renvoie JAMAIS l'api_key brute — masque + 4 derniers chars.
        keys = []
        for k in db.list_platform_keys():
            ak = k.get("api_key") or ""
            keys.append({
                "id": k["id"],
                "provider": k["provider"],
                "label": k["label"],
                "api_key_tail": ak[-4:] if len(ak) >= 4 else "",
                "created_at": k["created_at"],
            })
        return _json(request, {"platform_keys": keys})

    async def admin_platform_key_create(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_super_admin(sub):
            return _json_error(request, 403, "forbidden")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        provider = (body.get("provider") or "").strip()
        label = (body.get("label") or "").strip()
        api_key = (body.get("api_key") or "").strip()
        if provider not in db.KEY_PROVIDERS:
            return _json_error(request, 400, "invalid_provider")
        if not label or not api_key:
            return _json_error(request, 400, "missing_fields")
        try:
            key_id = db.create_platform_key(provider, label, api_key)
        except ValueError:
            return _json_error(request, 409, "duplicate_label")
        return _json(request, {"id": key_id, "provider": provider, "label": label})

    async def admin_platform_key_delete(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_super_admin(sub):
            return _json_error(request, 403, "forbidden")
        try:
            key_id = int(request.path_params["key_id"])
        except (ValueError, KeyError):
            return _json_error(request, 400, "invalid_id")
        if not db.get_platform_key(key_id):
            return _json_error(request, 404, "unknown_key")
        db.delete_platform_key(key_id)
        return _json(request, {"ok": True, "id": key_id})

    async def admin_tokens_list(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_super_admin(sub):
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        if not db.get_user(target_sub):
            return _json_error(request, 404, "unknown_user")
        return _json(request, {"tokens": db.list_api_tokens(target_sub)})

    async def admin_tokens_create(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_super_admin(sub):
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        if not db.get_user(target_sub):
            return _json_error(request, 404, "unknown_user")
        try:
            body = await request.json()
        except Exception:
            body = {}
        label = (body or {}).get("label") or "cli"
        ttl_raw = (body or {}).get("ttl_days")
        ttl_days = int(ttl_raw) if isinstance(ttl_raw, (int, str)) and str(ttl_raw).isdigit() else None
        token = db.create_api_token(target_sub, label=label.strip()[:32], ttl_days=ttl_days)
        return _json(request, {"token": token, "label": label, "ttl_days": ttl_days})

    async def admin_tokens_delete(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_super_admin(sub):
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        try:
            token_id = int(request.path_params["token_id"])
        except ValueError:
            return _json_error(request, 400, "invalid_id")
        ok = db.delete_api_token(target_sub, token_id)
        if not ok:
            return _json_error(request, 404, "unknown_token")
        return _json(request, {"ok": True, "id": token_id})

    async def admin_monitoring_summary(request: Request) -> JSONResponse:
        """Agrégats des appels MCP (total / échecs / par tool / par user / par
        jour) sur une fenêtre `?days=` (défaut 7). Admin only."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_platform_operator(sub):
            return _json_error(request, 403, "forbidden")
        try:
            days = int(request.query_params.get("days", "7"))
        except ValueError:
            days = 7
        return _json(request, db.tool_call_stats(since_days=days))

    async def admin_monitoring_calls(request: Request) -> JSONResponse:
        """Derniers appels MCP (journal brut), récent d'abord. Filtres :
        `?limit=` (défaut 200, max 1000), `?sub=`, `?tool=`, `?errors=1`,
        `?days=`. Admin only."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_platform_operator(sub):
            return _json_error(request, 403, "forbidden")
        qp = request.query_params
        try:
            limit = int(qp.get("limit", "200"))
        except ValueError:
            limit = 200
        since_days: int | None = None
        if qp.get("days"):
            try:
                since_days = int(qp["days"])
            except ValueError:
                since_days = None
        calls = db.list_tool_calls(
            limit=limit,
            sub=qp.get("sub") or None,
            tool_name=qp.get("tool") or None,
            errors_only=qp.get("errors") in ("1", "true"),
            since_days=since_days,
        )
        return _json(request, {"calls": calls})

    def _monitoring_days(request: Request, default: int = 7) -> int:
        try:
            return int(request.query_params.get("days", str(default)))
        except ValueError:
            return default

    async def admin_monitoring_rest(request: Request) -> JSONResponse:
        """Lentille REST (ADR 0017, kind='rest') : volume/erreurs/latence des appels
        `/api/*` par route, sur `?days=` (défaut 7). Admin only."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_platform_operator(sub):
            return _json_error(request, 403, "forbidden")
        return _json(request, db.rest_call_stats(since_days=_monitoring_days(request)))

    async def admin_monitoring_connectors(request: Request) -> JSONResponse:
        """Santé connecteurs (ADR 0017, kind='connector') : échecs de résolution de
        credential par provider, sur `?days=` (défaut 7). Admin only."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_platform_operator(sub):
            return _json_error(request, 403, "forbidden")
        return _json(request, db.connector_failure_stats(since_days=_monitoring_days(request)))

    async def admin_monitoring_funnel(request: Request) -> JSONResponse:
        """Funnel d'activation : comptes vs usage réel (idle / jamais actif / bloqué
        connecteur), fenêtre `?days=` (défaut 30). Admin only."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if not access.is_platform_operator(sub):
            return _json_error(request, 403, "forbidden")
        return _json(request, db.activation_funnel(active_window_days=_monitoring_days(request, 30)))

    async def my_calls(request: Request) -> JSONResponse:
        """Journal des appels MCP de l'utilisateur courant (sa propre activité).
        Filtres `?limit=`/`?tool=`/`?errors=1`/`?days=`. Scopé au sub du token ET à
        l'**org active** (consultation `X-Oto-Org` ?? maison, seam `current_org`, ADR 0023)
        — un user ne voit QUE ses propres appels DANS l'org chargée (≠ /api/admin/monitoring
        qui agrège tout le monde et reste admin-only)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        qp = request.query_params
        try:
            limit = int(qp.get("limit", "200"))
        except ValueError:
            limit = 200
        since_days: int | None = None
        if qp.get("days"):
            try:
                since_days = int(qp["days"])
            except ValueError:
                since_days = None
        calls = db.list_tool_calls(
            limit=limit,
            sub=sub,
            org_id=access.current_org(sub),
            tool_name=qp.get("tool") or None,
            errors_only=qp.get("errors") in ("1", "true"),
            since_days=since_days,
        )
        return _json(request, {"calls": calls})

    async def my_tools_list(request: Request) -> JSONResponse:
        """Liste tous les tools du serveur avec l'état (enabled/disabled)
        pour l'utilisateur courant.
        """
        sub, err = await _authenticate(request, verifier)
        if err:
            return err

        all_names: set[str] = set()
        if mcp_instance is not None:
            # run_middleware=False : appelé hors session MCP (contexte REST), la
            # chaîne de middleware n'a pas de Context FastMCP et lèverait → on
            # veut la liste statique complète, le filtrage disabled est fait
            # juste après via `disabled`. (cf. _list_all_tool_names)
            tools = await mcp_instance.list_tools(run_middleware=False)
            all_names = {t.name for t in tools}

        disabled = set(db.list_user_disabled_tools(sub, access.current_org(sub) or 0))
        # Le middleware retire déjà les disabled de `list_tools` selon le sub
        # courant (celui de la requête REST = même token). On ré-ajoute donc
        # les disabled pour avoir la vue complète.
        all_names |= disabled

        return _json(request, {
            "tools": [
                {"name": n, "enabled": n not in disabled,
                 "protected": n in PROTECTED_TOOLS}
                for n in sorted(all_names)
            ],
        })

    async def my_tools_registry(request: Request) -> JSONResponse:
        """Registre résolu des tools exposés (ADR 0014) : nom + description
        (1ʳᵉ ligne de la docstring = champ MCP `description`, source de vérité du
        modèle) + source `native`/`federated`. Alimente la résolution des
        marqueurs `<tool:slug>` d'une doctrine, l'autocomplétion et le manifeste
        « outils référencés ». Les namespaces grant-only (bridges) sont exclus."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        try:
            reg = await tool_registry.build_registry(mcp_instance)
        except Exception as e:
            return _json_error(request, 500, f"list_tools_failed:{e}")
        out = sorted(reg.values(), key=lambda e: e["name"])
        return _json(request, {"tools": out, "count": len(out)})

    async def my_tools_disable(request: Request) -> JSONResponse:
        """Désactive un tool pour l'utilisateur courant (live)."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        if name in PROTECTED_TOOLS:
            return _json_error(request, 400, f"protected_tool:{name}")
        org = access.current_org(sub) or 0
        db.add_user_disabled_tool(sub, name, org)
        db.remove_user_enabled_tool(sub, name, org)  # lève un éventuel override positif
        return _json(request, {"ok": True, "name": name, "enabled": False})

    async def my_tools_enable(request: Request) -> JSONResponse:
        """Réactive un tool pour l'utilisateur courant (live).

        Visibilité-only (ADR 0031) — même modèle que le meta-tool `oto_enable_tool` :
        activer = préférence d'affichage, pas une autorisation (accès réel gardé au
        call-time : credential + require_connector_access ADR 0025 + activation).
        """
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        org = access.current_org(sub) or 0
        db.remove_user_disabled_tool(sub, name, org)
        # Override positif requis pour rendre visible un masqué-par-défaut.
        if is_default_hidden(name):
            db.add_user_enabled_tool(sub, name, org)
        return _json(request, {"ok": True, "name": name, "enabled": True})

    async def _tool_by_name(name: str):
        """Objet Tool FastMCP par nom (ou None). `run_middleware=False` : hors
        session MCP (contexte REST) la chaîne de middleware n'a pas de Context."""
        if mcp_instance is None:
            return None
        tools = await mcp_instance.list_tools(run_middleware=False)
        for t in tools:
            if t.name == name:
                return t
        return None

    async def my_tool_detail(request: Request) -> JSONResponse:
        """Fiche d'un outil : description complète + schémas d'entrée/sortie
        (JSON Schema dérivé par FastMCP) + connecteur + état perso + testabilité.

        Alimente le panneau « en savoir plus » de la fiche connecteur (dashboard) —
        détail utile pour comprendre un outil (surtout open-data FOD) et, s'il est
        testable, générer un formulaire de test."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        tool = await _tool_by_name(name)
        if tool is None:
            return _json_error(request, 404, f"unknown_tool:{name}")
        ns = namespace_of(name)
        conn = connectors.connector_for_namespace(ns)
        disabled = set(db.list_user_disabled_tools(sub, access.current_org(sub) or 0))
        federated = bool(conn and conn.kind == "mount")
        return _json(request, {
            "name": name,
            "description": (tool.description or "").strip(),
            "input_schema": getattr(tool, "parameters", None),
            "output_schema": getattr(tool, "output_schema", None),
            "namespace": ns,
            "connector": ({"name": conn.name, "label": conn.label} if conn else None),
            "source": "federated" if federated else "native",
            "enabled": name not in disabled,
            "protected": name in PROTECTED_TOOLS,
            "default_hidden": is_default_hidden(name),
            "testable": is_testable(name),
        })

    async def my_tool_call(request: Request) -> JSONResponse:
        """Exécute un outil TESTABLE sous l'identité de l'appelant (bouton « tester »
        du dashboard). Bornée aux connecteurs open-data en lecture seule
        (`is_testable`) — jamais un outil à effet de bord. Les gates de call-time
        (credential, RBAC connecteur, activation) s'appliquent normalement : le
        sub-override REST fait résoudre la bonne identité (`resolve_api_key`/
        `current_org`). L'erreur d'un outil est renvoyée EN DONNÉE (`ok:false`) —
        voir ce que renvoie l'outil (y compris son erreur) EST le but du test."""
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        name = request.path_params["name"]
        if not is_testable(name):
            return _json_error(request, 403, f"not_testable:{name}")
        tool = await _tool_by_name(name)
        if tool is None:
            return _json_error(request, 404, f"unknown_tool:{name}")
        fn = getattr(tool, "fn", None)
        if fn is None:
            return _json_error(request, 400, f"not_callable:{name}")
        try:
            body = await request.json()
        except Exception:
            body = {}
        # Accepte {"arguments": {...}} ou l'objet d'arguments brut.
        args = body.get("arguments") if isinstance(body, dict) and "arguments" in body else body
        if not isinstance(args, dict):
            args = {}

        async def _invoke():
            if asyncio.iscoroutinefunction(fn):
                return await fn(**args)
            return await run_in_threadpool(lambda: fn(**args))

        started = time.monotonic()
        with auth_hooks.sub_override(sub):
            try:
                result = await asyncio.wait_for(_invoke(), timeout=45)
            except asyncio.TimeoutError:
                return _json(request, {"ok": False, "name": name,
                                       "error": "timeout (>45s)"})
            except TypeError as e:
                # Mauvais arguments (param inconnu / manquant) : signal actionnable.
                return _json_error(request, 400, f"bad_arguments:{e}")
            except Exception as e:  # noqa: BLE001 — l'erreur d'outil est le résultat
                return _json(request, {"ok": False, "name": name, "error": str(e)})
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Sérialisation défensive : un tool peut renvoyer un objet non-JSON.
        try:
            safe = json.loads(json.dumps(result, default=str, ensure_ascii=False))
        except Exception:
            safe = str(result)
        return _json(request, {"ok": True, "name": name, "result": safe,
                               "elapsed_ms": elapsed_ms})

    datastore_routes = api_routes_datastore.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        cors_headers=_cors_headers,
        options_handler=options_handler,
    )

    sirene_routes = api_routes_sirene.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    memento_routes = api_routes_memento.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    atlassian_routes = api_routes_atlassian.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    folk_routes = api_routes_folk.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    # Couche capacité (ADR 0009) : routes REST dérivées du registre (no-op tant
    # qu'il est vide — canari). Même séquence autz→validation→handler que MCP.
    capability_routes = _cap_rest_adapter.make_routes(
        verifier, _authenticate, _json, _json_error, options_handler,
        _cap_registry.CAPABILITIES,
    )

    # Cran d'activation des connecteurs (ADR 0010, B4) — admin only.
    connectors_routes = api_routes_connectors.make_routes(
        verifier, _authenticate, _json, _json_error, options_handler,
    )

    # Formulaire de contact public d'otomata.tech (non authentifié).
    contact_routes = api_routes_contact.make_routes(
        _json, _json_error, options_handler,
    )

    return [
        Route("/api/mcp/catalog", mcp_catalog, methods=["GET"]),
        Route("/api/mcp/catalog", options_handler, methods=["OPTIONS"]),
        Route("/api/connectors", connectors_catalog, methods=["GET"]),
        Route("/api/connectors", options_handler, methods=["OPTIONS"]),
        Route("/api/doctrines/library", doctrines_library_public, methods=["GET"]),
        Route("/api/doctrines/library", options_handler, methods=["OPTIONS"]),
        Route("/api/doctrines/library/{slug}", doctrines_library_public_get, methods=["GET"]),
        Route("/api/doctrines/library/{slug}", options_handler, methods=["OPTIONS"]),
        Route("/api/invitations/code/{code}", invite_preview_by_code, methods=["GET"]),
        Route("/api/invitations/code/{code}", options_handler, methods=["OPTIONS"]),
        Route("/api/invitations/referral/{carrier}", referral_preview, methods=["GET"]),
        Route("/api/invitations/referral/{carrier}", options_handler, methods=["OPTIONS"]),
        Route("/api/invitations/{token}", invite_preview, methods=["GET"]),
        Route("/api/invitations/{token}", options_handler, methods=["OPTIONS"]),
        Route("/api/me", me, methods=["GET"]),
        Route("/api/me", options_handler, methods=["OPTIONS"]),
        Route("/api/me/avatar", avatar_save, methods=["POST"]),
        Route("/api/me/avatar", avatar_clear, methods=["DELETE"]),
        Route("/api/me/avatar", options_handler, methods=["OPTIONS"]),
        Route("/api/me/projects/{project_id:int}/files", project_files_list, methods=["GET"]),
        Route("/api/me/projects/{project_id:int}/files", project_files_upload, methods=["POST"]),
        Route("/api/me/projects/{project_id:int}/files", options_handler, methods=["OPTIONS"]),
        Route("/api/me/projects/{project_id:int}/files/{file_id:int}", project_file_delete, methods=["DELETE"]),
        Route("/api/me/projects/{project_id:int}/files/{file_id:int}", options_handler, methods=["OPTIONS"]),
        Route("/api/me/projects/{project_id:int}/files/{file_id:int}/public", project_file_public, methods=["POST"]),
        Route("/api/me/projects/{project_id:int}/files/{file_id:int}/public", options_handler, methods=["OPTIONS"]),
        Route("/api/public/docs/{token}", public_doc, methods=["GET"]),
        Route("/api/public/docs/{token}", options_handler, methods=["OPTIONS"]),
        # Réception d'un upload signé out-of-bande (#105) — jeton dans l'URL, pas de JWT.
        # PUT/POST = agent (curl brut) / formulaire humain (multipart) ; GET = page d'upload.
        Route("/api/upload/{token}", upload_receive, methods=["PUT", "POST"]),
        Route("/api/upload/{token}", upload_form, methods=["GET"]),
        Route("/api/upload/{token}", options_handler, methods=["OPTIONS"]),
        # Page de partage publique server-rendered (lisible par un agent, ADR gap
        # « pages SPA non lisibles »). Servie sous dashboard.oto.ninja via Caddy.
        Route("/p/d/{token}", public_doc_view, methods=["GET"]),
        Route("/api/orgs/{id}/logo", org_logo_save, methods=["POST"]),
        Route("/api/orgs/{id}/logo", org_logo_clear, methods=["DELETE"]),
        Route("/api/orgs/{id}/logo", options_handler, methods=["OPTIONS"]),
        Route("/api/me/calls", my_calls, methods=["GET"]),
        Route("/api/me/calls", options_handler, methods=["OPTIONS"]),
        Route("/api/me/tools", my_tools_list, methods=["GET"]),
        Route("/api/me/tools", options_handler, methods=["OPTIONS"]),
        # `registry` AVANT `{name}` sinon Starlette le capture comme nom de tool.
        Route("/api/me/tools/registry", my_tools_registry, methods=["GET"]),
        Route("/api/me/tools/registry", options_handler, methods=["OPTIONS"]),
        Route("/api/me/tools/{name}", my_tools_disable, methods=["POST"]),
        Route("/api/me/tools/{name}", my_tools_enable, methods=["DELETE"]),
        Route("/api/me/tools/{name}", options_handler, methods=["OPTIONS"]),
        # Fiche + test d'un outil (dashboard) — suffixes distincts de `{name}` nu.
        Route("/api/me/tools/{name}/detail", my_tool_detail, methods=["GET"]),
        Route("/api/me/tools/{name}/detail", options_handler, methods=["OPTIONS"]),
        Route("/api/me/tools/{name}/call", my_tool_call, methods=["POST"]),
        Route("/api/me/tools/{name}/call", options_handler, methods=["OPTIONS"]),
        # /api/me/instructions* — migré en capacités (ADR 0009, capabilities/orgs_instructions.py),
        # monté par capability_routes plus bas.
        Route("/api/settings/api-keys/{provider}", api_key_get, methods=["GET"]),
        Route("/api/settings/api-keys/{provider}", api_key_save, methods=["POST"]),
        Route("/api/settings/api-keys/{provider}", api_key_clear, methods=["DELETE"]),
        Route("/api/settings/api-keys/{provider}", options_handler, methods=["OPTIONS"]),
        # Connexion par session navigateur (brevo/crunchbase) — Live View depuis le dashboard.
        Route("/api/me/connectors/{name}/session/start", session_start, methods=["POST"]),
        Route("/api/me/connectors/{name}/session/start", options_handler, methods=["OPTIONS"]),
        Route("/api/me/connectors/{name}/session/finalize", session_finalize, methods=["POST"]),
        Route("/api/me/connectors/{name}/session/finalize", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/platform-keys", admin_platform_keys_list, methods=["GET"]),
        Route("/api/admin/platform-keys", admin_platform_key_create, methods=["POST"]),
        Route("/api/admin/platform-keys", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/platform-keys/{key_id}", admin_platform_key_delete, methods=["DELETE"]),
        Route("/api/admin/platform-keys/{key_id}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/tokens", admin_tokens_list, methods=["GET"]),
        Route("/api/admin/users/{sub}/tokens", admin_tokens_create, methods=["POST"]),
        Route("/api/admin/users/{sub}/tokens", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/tokens/{token_id}", admin_tokens_delete, methods=["DELETE"]),
        Route("/api/admin/users/{sub}/tokens/{token_id}", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/monitoring/summary", admin_monitoring_summary, methods=["GET"]),
        Route("/api/admin/monitoring/summary", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/monitoring/calls", admin_monitoring_calls, methods=["GET"]),
        Route("/api/admin/monitoring/calls", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/monitoring/rest", admin_monitoring_rest, methods=["GET"]),
        Route("/api/admin/monitoring/rest", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/monitoring/connectors", admin_monitoring_connectors, methods=["GET"]),
        Route("/api/admin/monitoring/connectors", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/monitoring/funnel", admin_monitoring_funnel, methods=["GET"]),
        Route("/api/admin/monitoring/funnel", options_handler, methods=["OPTIONS"]),
        *datastore_routes,
        *sirene_routes,
        *memento_routes,
        *atlassian_routes,
        *folk_routes,
        *capability_routes,
        *connectors_routes,
        *contact_routes,
    ]
