"""ASSEMBLAGE de l'API REST `/api/*` — la table de routes, et rien d'autre.

Depuis la découpe du 2026-08-27, ce fichier ne contient plus de handler : il
**monte**. `make_routes` fait trois choses, dans cet ordre — appeler les
`make_routes` des modules de routes historiques, monter la couche capacité
(ADR 0009, deux faces générées d'un descripteur unique), et rendre la table
ordonnée des chemins écrits à la main. L'ORDRE de cette table est un contrat
(Starlette prend le PREMIER match : `…/tools/registry` doit précéder
`…/tools/{name}`), donc elle se lit d'un seul endroit.

Les handlers vivent par DOMAINE, chacun une fonction de module appelable seule :

| module                     | domaine                                              |
| -------------------------- | ---------------------------------------------------- |
| `api/base.py`       | primitives partagées (auth, CORS, JSON, OPTIONS, `bind`) |
| `api/public.py`     | surfaces sans auth : favicon, catalogues, bibliothèques, invitations, docs partagés |
| `api/media.py`      | avatar user, logo d'org (multipart)                   |
| `api/projects.py`   | fichiers bruts d'un projet, export ZIP                |
| `api/uploads.py`    | réception d'un upload signé (`/api/upload/{token}`)   |

Les modules ANTÉRIEURS à la découpe gardent leur forme : datastore, sirene,
accords, atlassian, folk, zoho, salesforce, billing — ils exposent un
`make_routes(...)` qui reçoit les primitives en paramètres. (`api/connectors.py` a
disparu le 2026-08-29 avec sa dernière route, le webhook de liaison messagerie :
dormant depuis la v2 du fournisseur, #581.)

Ce fichier garde aussi les deux MIDDLEWARES ASGI de la face REST, dont l'ordre de
pose (dans `server.py`) est un contrat dont dépendent des colonnes de monitoring :
`ViewAsMiddleware` (org/équipe/user de consultation, ADR 0023) et `RestCallLogger`
(une ligne `tool_calls(kind='rest')` par requête, ADR 0017).

Le reste du palier ORG (`/api/me/orgs`, `/api/orgs/*`, `/api/admin/orgs/*`) est
100 % en capacités depuis la migration qui a supprimé `api_routes_orgs.py` — ce
docstring y renvoyait encore le 2026-08-27, vers un fichier qui n'existe plus.

Auth : Bearer JWT Logto **ou** jeton API long-lived (préfixe `oto_`), vérifié par
`api_routes_base._authenticate` (ré-exporté ici). CORS : origines oto.cx/oto.ninja
(+ localhosts en dev), `_allowed_origins`.
"""
from __future__ import annotations

from typing import Iterable

import asyncio
import base64
import json
import logging
import time

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

from .. import db, journal_secrets, tenancy
from . import (accords as api_routes_accords,
               atlassian as api_routes_atlassian,
               billing as api_routes_billing,
               datastore as api_routes_datastore,
               folk as api_routes_folk,
               salesforce as api_routes_salesforce,
               sirene as api_routes_sirene,
               zoho as api_routes_zoho)
from ..capabilities import _rest_adapter as _cap_rest_adapter
from ..capabilities import registry as _cap_registry
# Primitives partagées (auth, CORS, réponses JSON, préflight, `bind`) : elles ont
# quitté ce fichier pour `base.py` le 2026-08-27, sous les modules de
# domaine qui les appellent (sinon l'import serait circulaire). RÉ-EXPORTÉES ici :
# `api.routes._authenticate` / `_cors_headers` / `_json` … restent valides.
from . import base as api_base
from .base import (  # noqa: F401 — ré-export de compatibilité
    AuthFn, _allowed_origins, _authenticate, _cors_headers, _json, _json_error,
    _maybe_view_as, bind, options_handler)
# Handlers par DOMAINE (découpe du 2026-08-27) : chaque module porte des fonctions
# de module, testables seules ; la table de routes ci-dessous reste ici.
from . import alias_routes, public
from . import media
from . import projects
from . import uploads

logger = logging.getLogger(__name__)




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


# Ops de LECTURE des endpoints op-aware (POST `{op:…}`). Le dashboard LIT en POST
# (`{op:'list'}`, `{op:'get'}`, …) — une garde par méthode HTTP bloquerait donc les
# lectures. En consultation LECTURE SEULE (view-as user / inspection org opérateur),
# seules ces ops passent sur une requête non-GET ; toute autre op — ou un POST/PUT/
# DELETE sans op (= action/upload) — est une écriture, rejetée. Deny-by-default :
# élargir cette liste si une vraie lecture op-aware manque.
_READ_OPS = frozenset({
    "list", "get", "search", "revisions", "list_changes", "inventory",
    "list_templates", "preview", "describe", "status",
})


async def _peek_op(receive):
    """Bufferise le corps de la requête, en extrait le champ `op` (JSON), et rend un
    `receive` qui REJOUE le corps intact au handler aval. Les routes `/api/*` sont de
    petites requêtes JSON → bufferiser est sûr (`/mcp` streaming est exclu en amont).
    Retourne `(op | None, receive_rejoué)`."""
    messages: list = []
    while True:
        msg = await receive()
        messages.append(msg)
        if msg.get("type") != "http.request" or not msg.get("more_body", False):
            break
    body = b"".join(m.get("body", b"") for m in messages if m.get("type") == "http.request")
    op = None
    if body:
        try:
            data = json.loads(body)
            op = data.get("op") if isinstance(data, dict) else None
        # noqa: SILENT — corps non-JSON légitime sur /mcp : la sonde d'op n'a rien à dire
        except Exception:
            op = None
    i = 0

    async def replay():
        nonlocal i
        if i < len(messages):
            m = messages[i]
            i += 1
            return m
        return {"type": "http.request", "body": b"", "more_body": False}

    return op, replay


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
        from .. import access, db, group_store, org_store, roles, session_org
        read_only = False  # consultation en LECTURE SEULE (view-as user OU inspection org opérateur)
        if view_user:  # « voir en tant que » : opérateur plateforme + cible existe + LECTURE SEULE
            if not await run_in_threadpool(access.is_platform_operator, sub):
                return await _json_error(request, 403, "forbidden")(scope, receive, send)
            if view_user == sub or await run_in_threadpool(db.get_user, view_user) is None:
                view_user = None  # cible = soi ou inconnue → pas de consultation (no-op)
            else:
                read_only = True
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
                # Opérateur plateforme NON-membre : inspection d'une org tierce en LECTURE
                # SEULE (même patron que le view-as user), même pour un super_admin (mode
                # inspection ≠ escalade d'admin).
                read_only = True
            else:
                return await _json_error(request, 403, "forbidden")(scope, receive, send)
        # Garde LECTURE SEULE : le dashboard LIT en POST op-aware (`{op:'list'|'get'}`),
        # donc on ne peut pas gater par méthode. Sur une requête non-GET, on lit l'`op`
        # du corps : seules les OPS DE LECTURE passent ; toute mutation (op d'écriture,
        # ou write sans op) → 403. Le corps est rejoué intact au handler.
        if read_only and request.method != "GET":
            op, receive = await _peek_op(receive)
            if op not in _READ_OPS:
                return await _json_error(request, 403, "view_as_read_only")(scope, receive, send)
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

_REST_LOG_TASKS: set = set()  # garde les refs des tâches fire-and-forget (anti-GC)


def _claimed_sub(request: Request) -> str | None:
    """Sub revendiqué par le bearer JWT, **NON vérifié** — attribution de log
    uniquement (jamais d'autz ; la route, elle, vérifie pour de vrai). Best-effort :
    token API opaque (`oto_…`) ou JWT malformé → None (ligne anonyme).

    Qualifié par tenant (ADR 0052) avec le MÊME qualificateur que le verifier : deux
    utilisateurs de deux émetteurs peuvent porter le même sub Logto, et sans ça leurs
    requêtes s'écriraient sur la même ligne d'audit — celle de l'utilisateur `oto`."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    parts = auth[7:].strip().split(".")
    if len(parts) != 3:  # pas un JWT → token opaque, pas d'attribution
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
        return tenancy.current().qualify_claims(claims)
    # noqa: SILENT — sub revendiqué illisible ⇒ pas de view-as, la requête reste la sienne
    except Exception:
        return None


def _normalize_route(path: str) -> str:
    """Réduit la cardinalité pour l'agrégation : segments d'id → `:id`, paramètres
    déclarés secrets → `:token` / `:code`. `/api/orgs/7/audit-log` →
    `/api/orgs/:id/audit-log`. Le fond vit dans `oto_mcp.journal_secrets` : la
    réduction PAR FORME (ce que faisait cette fonction) ne voyait pas les quatre
    routes dont le secret est dans le chemin (#558)."""
    return journal_secrets.route_and_secrets(path)[0]


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
        org = _parse_view_org(request)  # org de consultation revendiquée (header), best-effort
        started = time.monotonic()
        try:
            await self.app(scope, receive, _send)
        finally:
            code = status["code"]
            # ⚠️ LU APRÈS la requête, pas avant : c'est l'authentification qui
            # résout le porteur, et elle n'a pas encore tourné au moment où le
            # middleware entre. Calculer le compte à l'entrée revenait à ne
            # pouvoir le lire que dans l'en-tête — donc à n'attribuer QUE les
            # JWT, et à écrire une ligne anonyme pour tout appel par jeton API ou
            # par jeton de délégation.
            principal = scope.get(api_base.CLE_PRINCIPAL) or {}
            sub = principal.get("sub") or _claimed_sub(request)
            route, masques = journal_secrets.route_and_secrets(scope.get("path", ""))
            row = {
                "kind": "rest",
                "tool": f"{method} {route}",
                # Le masque ne va JAMAIS dans `tool` (une empreinte par jeton ferait
                # exploser la cardinalité du `GROUP BY tool` du monitoring) : il va
                # dans `args`, où il répond à « le même jeton a-t-il été rejoué ? ».
                "args": masques,
                "sub": sub,
                "effective_sub": principal.get("effective_sub"),
                # Le jeton employé, NOMMÉ jamais écrit : deux appels du même compte
                # par deux jetons étaient indistinguables, et une délégation du
                # runner ressemblait à une session humaine.
                "token_id": principal.get("token_id"),
                "token_kind": principal.get("token_kind"),
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

    accords_routes = api_routes_accords.make_routes(
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

    # OAuth Zoho « server-based » — SECOND mode d'acquisition, le Self Client
    # restant intact et par défaut (les deux produisent le même credential).
    zoho_routes = api_routes_zoho.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    salesforce_oauth_routes = api_routes_salesforce.make_routes(
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

    # Billing écrit à la main (ADR 0043, #488) : le webhook Mollie (non authentifié)
    # et le téléchargement du PDF d'une facture (authentifié — un octet n'est pas du
    # JSON, il ne peut donc pas passer par la couche capacité).
    billing_webhook_routes = api_routes_billing.make_routes(
        options_handler, verifier=verifier, authenticate=_authenticate,
        json_error=_json_error)

    table = [
        Route("/favicon.svg", public.favicon, methods=["GET"]),
        Route("/favicon.ico", public.favicon, methods=["GET"]),
        # La version SERVIE (oto#33), sans auth : ce que CE processus exécute. La
        # même étiquette part sur chaque réponse en `X-Oto-Version` — l'endpoint
        # sert à qui demande, l'en-tête à qui relit son journal après coup.
        Route("/api/version", public.version, methods=["GET"]),
        Route("/api/version", options_handler, methods=["OPTIONS"]),
        Route("/api/mcp/catalog", bind(public.mcp_catalog, mcp_instance=mcp_instance), methods=["GET"]),
        Route("/api/mcp/catalog", options_handler, methods=["OPTIONS"]),
        # Descriptif de l'API REST, dérivé (cf. openapi.py). Servi aux deux chemins
        # usuels : un intégrateur sonde l'un ou l'autre, aucun n'est plus canonique.
        Route("/openapi.json", public.openapi_doc, methods=["GET"]),
        Route("/openapi.json", options_handler, methods=["OPTIONS"]),
        Route("/api/openapi.json", public.openapi_doc, methods=["GET"]),
        Route("/api/openapi.json", options_handler, methods=["OPTIONS"]),
        Route("/api/connectors", bind(public.connectors_catalog, verifier=verifier), methods=["GET"]),
        Route("/api/connectors", options_handler, methods=["OPTIONS"]),
        # ⚠️ DEUX bibliothèques, deux objets : `/api/guide-library` = le MARCHÉ des
        # guides publiés par les orgs (vue `guide_library`, forkables) ;
        # `/api/guides/library` = les guides PLATEFORME (table `guides`). Les noms se
        # ressemblaient déjà avant ce lot — ils ne désignent pas la même chose.
        Route("/api/guide-library", public.guide_library_public, methods=["GET"]),
        Route("/api/guide-library", options_handler, methods=["OPTIONS"]),
        Route("/api/guide-library/{slug}", public.guide_library_public_get, methods=["GET"]),
        Route("/api/guide-library/{slug}", options_handler, methods=["OPTIONS"]),
        Route("/api/guides/library", public.guides_library_public, methods=["GET"]),
        Route("/api/guides/library", options_handler, methods=["OPTIONS"]),
        Route("/api/guides/library/{slug}", public.guides_library_public_get, methods=["GET"]),
        Route("/api/guides/library/{slug}", options_handler, methods=["OPTIONS"]),
        Route("/api/invitations/code/{code}", public.invite_preview_by_code, methods=["GET"]),
        Route("/api/invitations/code/{code}", options_handler, methods=["OPTIONS"]),
        Route("/api/invitations/{token}", public.invite_preview, methods=["GET"]),
        Route("/api/invitations/{token}", options_handler, methods=["OPTIONS"]),
        Route("/api/me/avatar", bind(media.avatar_save, verifier=verifier), methods=["POST"]),
        Route("/api/me/avatar", options_handler, methods=["OPTIONS"]),
        Route("/api/me/projects/{project_id:int}/files", bind(projects.project_files_upload, verifier=verifier), methods=["POST"]),
        Route("/api/me/projects/{project_id:int}/files", options_handler, methods=["OPTIONS"]),
        Route("/api/public/docs/{token}", public.public_doc, methods=["GET"]),
        Route("/api/public/docs/{token}", options_handler, methods=["OPTIONS"]),
        # Réception d'un upload signé out-of-bande (#105) — jeton dans l'URL, pas de JWT.
        # PUT/POST = agent (curl brut) / formulaire humain (multipart) ; GET = page d'upload.
        Route("/api/upload/{token}", uploads.upload_receive, methods=["PUT", "POST"]),
        Route("/api/upload/{token}", uploads.upload_form, methods=["GET"]),
        Route("/api/upload/{token}", options_handler, methods=["OPTIONS"]),
        # Page de partage publique server-rendered (lisible par un agent, ADR gap
        # « pages SPA non lisibles »). Servie sous dashboard.oto.ninja via Caddy.
        Route("/p/d/{token}", public.public_doc_view, methods=["GET"]),
        # Désinscription des relances (oto_admin_outreach) — jeton signé dans
        # l'URL, aucune session : c'est le lien du pied de page des mails.
        Route("/o/u/{token}", public.outreach_unsubscribe, methods=["GET"]),
        Route("/api/orgs/{id}/logo", bind(media.org_logo_save, verifier=verifier), methods=["POST"]),
        Route("/api/orgs/{id}/logo", options_handler, methods=["OPTIONS"]),
        # /api/me/instructions* — migré en capacités (ADR 0009, capabilities/orgs/instructions.py),
        # monté par capability_routes plus bas.
        Route("/api/me/projects/{id}/export", bind(projects.me_project_export, verifier=verifier), methods=["GET"]),
        Route("/api/me/projects/{id}/export", options_handler, methods=["OPTIONS"]),
        *datastore_routes,
        *sirene_routes,
        *accords_routes,
        *atlassian_routes,
        *folk_routes,
        *zoho_routes,
        *salesforce_oauth_routes,
        *capability_routes,
        *billing_webhook_routes,
        # EN DERNIER, et c'est la garde : un alias déprécié ne peut capturer que ce
        # que rien d'autre ne sert. Monté plus haut, un de ses placeholders pourrait
        # éclipser une vraie route sans que rien ne le dise (#519, retrait #526).
        *alias_routes.make_routes(options_handler),
    ]
    # Le journal apprend ICI quels paramètres de route portent un secret — dérivé
    # de la table qu'on vient d'assembler, jamais d'une liste tenue à la main (#558).
    # Une route future qui déclare `{token}` est couverte le jour où elle est montée.
    journal_secrets.declare_routes(table)
    return table
