"""Middlewares FastMCP — application des préférences user au boot de session."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware
from fastmcp.server.transforms.visibility import disable_components

from . import access, db
from .auth_hooks import current_user_sub_from_token
from .tool_visibility import DEFAULT_HIDDEN_TOOLS, effective_disabled

logger = logging.getLogger(__name__)


class UserDisabledToolsMiddleware(Middleware):
    """Applique la visibilité des tools du user à sa session MCP.

    Au handshake `initialize`, pour le `sub` JWT courant, on calcule
    l'ensemble effectif des tools à masquer = `user_disabled_tools` ∪
    (tools masqués par défaut non explicitement activés). On pose une
    visibility rule session-scopée via `disable_components`. Le reste —
    filtrage `tools/list`, blocage `tools/call`, émission de
    `tools/list_changed` — est géré nativement par fastmcp.

    Pas de sub identifiable (stdio local, discovery non-authentifié) → on ne
    filtre rien : la machine du dev a accès complet, le masquage par défaut
    ne concerne que la surface multi-user authentifiée.
    """

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        try:
            sub = current_user_sub_from_token()
        except Exception:
            sub = None
        if not sub:
            return result
        ctx = context.fastmcp_context
        if ctx is None:
            logger.warning("fastmcp_context is None at on_initialize for sub=%s", sub)
            return result
        try:
            disabled = set(db.list_user_disabled_tools(sub))
            enabled_override = set(db.list_user_enabled_tools(sub))
            granted = frozenset(db.list_user_granted_namespaces(sub))
        except Exception as e:
            logger.warning("Cannot read tool visibility for %s: %s", sub, e)
            return result
        try:
            is_admin = access.get_user_role(sub) == access.ADMIN
        except Exception:
            is_admin = False
        try:
            all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
            all_names = {t.name for t in all_tools}
        except Exception as e:
            logger.warning("Cannot list tools for %s: %s", sub, e)
            # repli : au moins les disabled explicites + les masqués connus
            all_names = disabled | DEFAULT_HIDDEN_TOOLS
        to_hide = effective_disabled(all_names, disabled, enabled_override, granted, is_admin)
        if not to_hide:
            return result
        try:
            await disable_components(ctx, names=to_hide, components={"tool"})
        except Exception as e:
            logger.warning("Failed to apply tool visibility for %s: %s", sub, e)
        return result
