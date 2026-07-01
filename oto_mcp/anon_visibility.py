"""Visibilité d'un endpoint MCP anonyme — allowlist FIGÉE (ADR 0032, amende #44).

Sur l'instance FastMCP anonyme (`<slug>.mcp.oto.cx`, sans auth), la visibilité n'est
PAS la denylist per-(sub, org) habituelle (il n'y a pas de sub) : c'est une **allowlist
figée** = les seuls tools du preset du projet (`AnonContext.tools`, posé par
`subdomain_project.HostDispatch`). Tout le reste est masqué **fail-CLOSED** — un endpoint
public ne doit JAMAIS exposer un tool hors preset. Miroir minimal d'`apply_session_visibility`.
"""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware
from fastmcp.server.transforms.visibility import disable_components

from . import subdomain_project

logger = logging.getLogger(__name__)

# Backstop FAIL-CLOSED : tous les noms de tools vus lors d'un list_tools réussi. Si le
# listing échoue (rare, in-memory), on masque au moins ce qu'on connaît (sinon la
# denylist serait incomplète → fuite publique).
_ALL_NAMES_CACHE: set[str] = set()


class AnonymousVisibilityMiddleware(Middleware):
    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        allow = subdomain_project.current_allowlist()
        if allow is None:
            # Pas un endpoint anonyme résolu → défensif : ne rien exposer si l'instance
            # anonyme est atteinte sans contexte (Host non résolu). Fail-CLOSED via cache.
            ctx = getattr(context, "fastmcp_context", None)
            if ctx is not None and _ALL_NAMES_CACHE:
                await disable_components(ctx, names=set(_ALL_NAMES_CACHE), components={"tool"})
            return result
        ctx = getattr(context, "fastmcp_context", None)
        if ctx is None:
            logger.warning("anon visibility: fastmcp_context is None")
            return result
        try:
            all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
            all_names = {t.name for t in all_tools}
            _ALL_NAMES_CACHE.update(all_names)
        except Exception as e:  # noqa: BLE001
            logger.warning("anon visibility: list_tools failed (fail-closed via cache): %s", e)
            all_names = set(_ALL_NAMES_CACHE)
        to_hide = all_names - set(allow)
        if to_hide:
            await disable_components(ctx, names=to_hide, components={"tool"})
        return result
