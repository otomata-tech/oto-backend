"""Endpoint scopé par org — `<slug>--mcp.oto.ninja` épingle l'org de la connexion.

Middleware ASGI **brut** (n'altère pas le streaming `/mcp` — lit le `Host`, pas le
body) : résout le slug → org et l'enregistre pour la requête (contextvar + dict
keyé par `mcp-session-id`, cf. session_org). La GARDE d'appartenance est appliquée
en aval par `access.current_org` (un non-membre retombe sur son org maison → zéro
fuite). Host canonique (`mcp.oto.ninja`) ou slug inconnu → pass-through total.

Résolution slug→org : par **nom normalisé** (canari). Productionisation =
colonne `orgs.slug` unique + index ; invalidation de cache.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlparse

from starlette.requests import Request

_CACHE: dict[str, int] = {}   # slug → org_id (seuls les hits sont cachés)


def _suffix() -> str:
    """Suffixe `--<host canonique>` (le label avant = le slug d'org). Dérivé du HOST
    canonique MCP (`OTO_MCP_PUBLIC_URL`) — PROD `mcp.oto.cx` / PREPROD `mcp.oto.ninja`
    (cutover ADR 0040) : plus de domaine figé (sinon l'épinglage d'org casse hors prod)."""
    host = urlparse(os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja")).hostname or "mcp.oto.ninja"
    return f"--{host}"


def _slug_from_host(host: str) -> Optional[str]:
    h = (host or "").split(":")[0].strip().lower()
    suffix = _suffix()
    if not h.endswith(suffix):
        return None
    return h[: -len(suffix)] or None


def _resolve_slug(slug: str) -> Optional[int]:
    from . import org_store
    try:
        for o in org_store.list_all_orgs():
            if org_store.normalize_slug(o.get("name") or "") == slug:
                return o.get("id")
    except Exception:
        return None
    return None


def org_id_for_host(host: str) -> Optional[int]:
    """org_id de l'endpoint scopé, ou None (host canonique / slug inconnu).
    Cache les seuls hits → un slug inconnu reste résolu à chaque requête (rare),
    et une org créée après le boot est prise en compte sans restart."""
    slug = _slug_from_host(host)
    if slug is None:
        return None
    if slug in _CACHE:
        return _CACHE[slug]
    oid = _resolve_slug(slug)
    if oid is not None:
        _CACHE[slug] = oid
    return oid


class SubdomainOrgMiddleware:
    """ASGI brut : enregistre l'org du sous-domaine pour la requête courante."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)  # headers seulement → ne consomme pas le body
        org_id = org_id_for_host(request.headers.get("host", ""))
        if org_id is None:
            return await self.app(scope, receive, send)
        from . import session_org
        token = session_org.set_subdomain_cv(org_id)
        sid = request.headers.get("mcp-session-id")
        if sid:
            session_org.store_subdomain_org(sid, org_id)
        try:
            return await self.app(scope, receive, send)
        finally:
            session_org.reset_subdomain_cv(token)
