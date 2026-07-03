"""Endpoint MCP par PROJET — `<slug>.mcp.oto.cx` (ADR 0032, amende #44).

Un projet publié (`mcp_access ∈ {anonymous, secret, org}`, cf. `db.set_project_mcp_publication`)
est servi sur un sous-domaine dédié. Trois postures :

- **anonymous** — AUCUN login. Le sous-domaine est servi par une **2ᵉ instance FastMCP
  sans auth** ; la visibilité y fige l'**allowlist** du preset (`AnonymousVisibilityMiddleware`)
  et la résolution de credential tape l'org **propriétaire** du projet (pas de `sub` —
  cf. `access._resolve_credential_anon`). Contourne 100 % du blocage OAuth de #44. LISTÉ
  dans l'annuaire public (`db.list_published_mcp_projects`).
- **secret** — identique à `anonymous` côté serving (même instance, même résolution), mais
  **non listé** dans l'annuaire et **slug non devinable** (généré serveur) : une URL secrète.
  Les deux propriétés vivent côté publication → transparent pour ce dispatch.
- **org** — JWT Logto + **épingle l'org** (comme `subdomain_org`, garde d'appartenance en
  aval par `access.current_org`). Servi par l'instance authentifiée canonique.

`HostDispatch` = l'app ASGI racine : résout le Host UNE fois, pose le contexte
(contextvar + dict keyé `mcp-session-id`, comme `session_org` — le contextvar ne
propage pas au threadpool des tools sync), puis délègue à l'app anonyme ou authentifiée.
Host canonique / slug inconnu / projet dé-publié → pass-through vers l'authentifiée.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from . import session_org

logger = logging.getLogger(__name__)

# Suffixe figé du domaine public (le label avant = le slug de projet).
_SUFFIX = ".mcp.oto.cx"

# Resource indicator (audience JWT) d'un endpoint org : `https://<slug>.mcp.oto.cx/mcp`.
_ORG_AUD_RE = re.compile(r"^https://([a-z0-9]([a-z0-9-]*[a-z0-9]))\.mcp\.oto\.cx/mcp/?$")


def valid_org_audience(aud: object) -> bool:
    """Un `aud` de token est-il le resource indicator d'un endpoint org PUBLIÉ ?
    (ADR 0032 barreau 4, #44). Vérifie le motif `<slug>.mcp.oto.cx/mcp` PUIS que le
    projet existe et est en `mcp_access='org'`. **Fail-closed** : toute erreur (DB
    indispo) → False (on rejette plutôt que d'accepter une audience non prouvée)."""
    if not isinstance(aud, str):
        return False
    m = _ORG_AUD_RE.match(aud)
    if not m:
        return False
    try:
        from . import db
        proj = db.get_project_by_mcp_slug(m.group(1))
        return bool(proj and proj.get("mcp_access") == "org")
    except Exception as e:  # noqa: BLE001
        logger.warning("valid_org_audience(%s) failed (fail-closed): %s", aud, e)
        return False


@dataclass(frozen=True)
class AnonContext:
    """Contexte d'un endpoint MCP anonyme résolu pour la requête courante."""
    project_id: int
    org_id: Optional[int]          # org propriétaire (résolution de credential) ; None si projet user-owned legacy
    tools: frozenset               # allowlist figée du preset (les seuls tools exposés)


# Contexte anonyme : contextvar (même requête, ex. l'initialize qui calcule la
# visibilité en async) + dict keyé mcp-session-id (lectures hors-contextvar des
# appels d'outils sync en threadpool). Borné pour ne pas fuir.
_CTX: contextvars.ContextVar[Optional[AnonContext]] = contextvars.ContextVar(
    "oto_anon_ctx", default=None)
_ANON_BY_SID: dict[str, AnonContext] = {}
_CAP = 100_000


def _store_ctx(sid: str, ctx: AnonContext) -> None:
    _ANON_BY_SID.pop(sid, None)
    _ANON_BY_SID[sid] = ctx
    while len(_ANON_BY_SID) > _CAP:
        del _ANON_BY_SID[next(iter(_ANON_BY_SID))]


# ── Rate-limit anti-abus (endpoint anonyme public, ADR 0032 barreau 3) ───────
# Un endpoint anonyme est ouvert au monde et consomme le quota/les clés de l'org
# propriétaire → token-bucket in-memory par (IP, projet). Pas une barrière de sécu
# (contournable par rotation d'IP) mais un garde-fou contre le hammering trivial ;
# le vrai plafond de coût reste le quota de la clé d'org. Défauts généreux (une
# session MCP = plusieurs requêtes : initialize + list + N calls). Tunables par env.
def _rate_per_min() -> float:
    try:
        return float(os.environ.get("OTO_ANON_RATE_PER_MIN", "120"))
    except ValueError:
        return 120.0


def _rate_burst() -> float:
    try:
        return float(os.environ.get("OTO_ANON_RATE_BURST", "60"))
    except ValueError:
        return 60.0


# key (ip, project_id) -> (tokens, last_ts). Borné (éviction du plus ancien).
_BUCKETS: dict[tuple[str, int], tuple[float, float]] = {}
_BUCKETS_CAP = 50_000


def _check_bucket(key: tuple[str, int], now: float) -> bool:
    """True = requête autorisée (jette un jeton). Refill continu au taux `per_min`,
    plafonné à `burst`. Now injecté → testable sans horloge réelle."""
    per_sec = _rate_per_min() / 60.0
    burst = _rate_burst()
    tokens, last = _BUCKETS.get(key, (burst, now))
    tokens = min(burst, tokens + (now - last) * per_sec)
    if tokens < 1.0:
        _BUCKETS[key] = (tokens, now)
        return False
    _BUCKETS[key] = (tokens - 1.0, now)
    while len(_BUCKETS) > _BUCKETS_CAP:
        del _BUCKETS[next(iter(_BUCKETS))]
    return True


def _client_ip(scope, headers: dict) -> str:
    """IP réelle du client derrière Cloudflare/Caddy : `CF-Connecting-IP` > 1er hop de
    `X-Forwarded-For` > IP de socket. Un endpoint anonyme est CF-proxied (ADR infra)."""
    cf = headers.get(b"cf-connecting-ip")
    if cf:
        return cf.decode("latin-1").strip()
    xff = headers.get(b"x-forwarded-for")
    if xff:
        return xff.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


async def _send_html(send, body: str) -> None:
    data = body.encode("utf-8")
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                            (b"cache-control", b"public, max-age=300")]})
    await send({"type": "http.response.body", "body": data})


async def _send_429(send) -> None:
    body = json.dumps({"error": "rate_limited",
                       "message": "Trop de requêtes sur cet endpoint anonyme. Réessaie plus tard."}).encode()
    await send({"type": "http.response.start", "status": 429,
                "headers": [(b"content-type", b"application/json"),
                            (b"retry-after", b"30")]})
    await send({"type": "http.response.body", "body": body})


def current_anon_context() -> Optional[AnonContext]:
    """Contexte anonyme de la requête/session courante, ou None (endpoint authentifié)."""
    v = _CTX.get()
    if v is not None:
        return v
    sid = session_org.current_session_id()
    return _ANON_BY_SID.get(sid) if sid else None


def current_anon_org() -> Optional[int]:
    """Org propriétaire de l'endpoint anonyme courant (seam pour `access.current_org(None)`)."""
    ctx = current_anon_context()
    return ctx.org_id if ctx else None


def current_allowlist() -> Optional[frozenset]:
    """Allowlist figée du preset anonyme courant, ou None (pas d'endpoint anonyme)."""
    ctx = current_anon_context()
    return ctx.tools if ctx else None


def _slug_from_host(host: str) -> Optional[str]:
    h = (host or "").split(":")[0].strip().lower()
    if not h.endswith(_SUFFIX):
        return None
    slug = h[: -len(_SUFFIX)]
    # Un seul label (pas de point) : `x.y.mcp.oto.cx` n'est pas un slug de projet.
    return slug if slug and "." not in slug else None


def resolve_project(host: str) -> Optional[dict]:
    """Projet publié pour ce Host, ou None (host canonique / slug inconnu / dé-publié).
    Pas de cache : la requête est indexée et le trafic anonyme faible ; une dé-publication
    (unpublish) prend effet immédiatement (pas de fuite d'un endpoint retiré)."""
    slug = _slug_from_host(host)
    if slug is None:
        return None
    try:
        from . import db
        return db.get_project_by_mcp_slug(slug)
    except Exception as e:  # DB indispo → pass-through, jamais de 500 sur le dispatch
        logger.warning("resolve_project(%s) failed: %s", slug, e)
        return None


def _root_to_mcp(scope: dict) -> dict:
    """Réécrit le chemin RACINE `/` (ou vide) vers `/mcp` — le MCP d'un sous-domaine
    dédié est servi à la racine. Copie le scope (jamais de mutation partagée). Les
    chemins propres (OAuth, well-known, /mcp) ne matchent pas `/` → renvoyés tels quels."""
    if scope.get("path") in ("", "/"):
        return {**scope, "path": "/mcp", "raw_path": b"/mcp"}
    return scope


# ── App ASGI racine : dispatch par Host (anonyme vs authentifié) ─────────────
class HostDispatch:
    """App ASGI racine. Compose les lifespans des DEUX instances FastMCP (chacune a
    son session-manager streamable_http à démarrer) et route chaque requête HTTP par
    Host. Lit le Host sans consommer le body → n'altère jamais le streaming /mcp."""

    def __init__(self, authed_app, anon_app):
        self.authed = authed_app
        self.anon = anon_app

    async def __call__(self, scope, receive, send):
        t = scope.get("type")
        if t == "lifespan":
            return await self._lifespan(scope, receive, send)
        if t != "http":
            return await self.authed(scope, receive, send)
        return await self._http(scope, receive, send)

    async def _lifespan(self, scope, receive, send):
        """Compose les lifespans des deux apps (pattern ASGI standard) : entre dans
        les deux `lifespan_context` au startup, en sort au shutdown."""
        async with contextlib.AsyncExitStack() as stack:
            message = await receive()
            assert message["type"] == "lifespan.startup"
            try:
                await stack.enter_async_context(
                    self.authed.router.lifespan_context(self.authed))
                await stack.enter_async_context(
                    self.anon.router.lifespan_context(self.anon))
            except Exception as e:  # noqa: BLE001
                await send({"type": "lifespan.startup.failed", "message": str(e)})
                raise
            await send({"type": "lifespan.startup.complete"})
            message = await receive()
            assert message["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    async def _http(self, scope, receive, send):
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1")
        proj = resolve_project(host)
        if proj is None:
            return await self.authed(scope, receive, send)
        sid = headers.get(b"mcp-session-id", b"").decode("latin-1") or None
        access_mode = proj.get("mcp_access")
        org_id = int(proj["owner_id"]) if proj.get("owner_type") == "org" else None

        if access_mode in ("anonymous", "secret"):
            # `secret` = même chemin sans login que `anonymous` (aucun sub, credential de
            # l'org propriétaire) ; il n'en diffère QUE par l'annuaire (non listé) et un slug
            # non devinable — deux propriétés portées côté publication, transparentes ici.
            # Navigateur (GET, Accept: text/html) sur la RACINE → landing HTML publique :
            # la MÊME URL sert la page de présentation ET le serveur MCP. Claude/Mistral
            # (POST, ou Accept event-stream) tombent dans le chemin MCP ci-dessous.
            if (scope.get("method") == "GET" and scope.get("path") in ("", "/")
                    and b"text/html" in headers.get(b"accept", b"").lower()):
                from . import anon_landing
                return await _send_html(send, anon_landing.render(
                    name=proj.get("name") or "", brief_md=proj.get("brief_md") or "",
                    tools=list(proj.get("mcp_tools") or []), connect_url=f"https://{host}"))
            # Garde-fou anti-abus : token-bucket par (IP, projet) avant tout travail.
            if not _check_bucket((_client_ip(scope, headers), int(proj["id"])), time.monotonic()):
                return await _send_429(send)
            ctx = AnonContext(int(proj["id"]), org_id,
                              frozenset(proj.get("mcp_tools") or []))
            tok = _CTX.set(ctx)
            if sid:
                _store_ctx(sid, ctx)
            try:
                # Sur un sous-domaine DÉDIÉ, le MCP est servi à la RACINE : claude.ai/
                # Mistral POSTent l'initialize sur l'URL nue (`…mcp.oto.cx`, sans `/mcp`)
                # → on réécrit `/`→`/mcp`. Les routes OAuth/well-known (chemins propres)
                # ne sont pas `/` → intactes ; `/mcp` explicite marche toujours.
                return await self.anon(_root_to_mcp(scope), receive, send)
            finally:
                _CTX.reset(tok)

        if access_mode == "org" and org_id is not None:
            # Épingle l'org (réemploi du seam sous-domaine → garde d'appartenance
            # dans access.current_org). Servi par l'app AUTHENTIFIÉE (JWT requis).
            tok = session_org.set_subdomain_cv(org_id)
            if sid:
                session_org.store_subdomain_org(sid, org_id)
            try:
                return await self.authed(scope, receive, send)
            finally:
                session_org.reset_subdomain_cv(tok)

        return await self.authed(scope, receive, send)


# ── Garde-fou on-demand TLS (Caddy `ask`) ────────────────────────────────────
# Caddy émet un cert on-demand pour `<slug>.mcp.oto.cx` UNIQUEMENT si ce endpoint
# répond 200 → borne l'émission aux slugs de projets PUBLIÉS (anti-abus du rate-limit
# Let's Encrypt : un tiers qui martèle `random.mcp.oto.cx` ne déclenche aucun cert).
# Route NON authentifiée (appelée par Caddy en localhost), montée avant le gate JWT.
def make_routes():
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    async def _tls_check(request):
        domain = request.query_params.get("domain", "")
        return (PlainTextResponse("ok") if resolve_project(domain) is not None
                else PlainTextResponse("not published", status_code=404))

    async def _public_mcp_projects(request):
        """Annuaire PUBLIC des endpoints MCP anonymes (consommé par oto-websites,
        cross-origin → CORS *). Sans auth, sans donnée sensible (projets déjà publics)."""
        from . import db
        out = []
        try:
            for p in db.list_published_mcp_projects():
                slug = p.get("mcp_slug")
                if not slug:
                    continue
                out.append({"slug": slug, "name": p.get("name"),
                            "brief": (p.get("brief_md") or "")[:400],
                            "tools": list(p.get("mcp_tools") or []),
                            "url": f"https://{slug}.mcp.oto.cx"})
        except Exception as e:  # noqa: BLE001
            logger.warning("public mcp-projects list failed: %s", e)
        return JSONResponse({"projects": out},
                            headers={"Access-Control-Allow-Origin": "*",
                                     "Cache-Control": "public, max-age=120"})

    return [
        Route("/api/mcp/tls-check", _tls_check, methods=["GET"]),
        Route("/api/public/mcp-projects", _public_mcp_projects, methods=["GET"]),
    ]
