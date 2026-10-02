"""`VerrouOrgMiddleware` — le verrou d'org d'un jeton de délégation, pour la requête.

Pose `verrou_org` (ContextVar) depuis les claims du jeton d'API qui authentifie la
requête MCP (`server._verify_api_token`), pour toute la requête : contexte d'appel,
gardes d'autorisation et handler la lisent. Sans jeton verrouillé, le verrou est posé
à None — rien ne survit d'une requête à l'autre.

**Sur `on_request`**, comme la pause de compte : `initialize`, `tools/list` et
`tools/call` résolvent tous l'org de l'appelant.
"""
from __future__ import annotations

from fastmcp.server.middleware import Middleware

from .. import verrou_org


def _verrou_du_jeton():
    """Le verrou porté par le jeton d'API de la requête, ou None (OAuth, hors requête,
    jeton non verrouillé)."""
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
    # noqa: SILENT — hors requête MCP authentifiée : aucun jeton, donc aucun verrou
    except Exception:
        return None
    claims = getattr(token, "claims", None) or {}
    return verrou_org.depuis_ligne(claims)


class VerrouOrgMiddleware(Middleware):
    async def on_request(self, context, call_next):
        jeton = verrou_org.poser(_verrou_du_jeton())
        try:
            return await call_next(context)
        finally:
            verrou_org.lever(jeton)
