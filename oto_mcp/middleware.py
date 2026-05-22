"""Middlewares FastMCP — application des préférences user au boot de session."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware
from fastmcp.server.transforms.visibility import disable_components

from . import db
from .auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)


class UserDisabledToolsMiddleware(Middleware):
    """Applique les tools désactivés du user à sa session MCP.

    Au handshake `initialize`, on lit la table `user_disabled_tools` pour le
    `sub` JWT courant et on pose une visibility rule session-scopée via
    `disable_components`. Le reste — filtrage `tools/list`, blocage
    `tools/call`, émission de `tools/list_changed` — est géré nativement par
    fastmcp.

    Si pas de sub identifiable (stdio local, discovery non-authentifié) ou
    aucune préférence en DB, on ne fait rien.
    """

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        try:
            sub = current_user_sub_from_token()
        except Exception:
            sub = None
        if not sub:
            return result
        try:
            disabled = db.list_user_disabled_tools(sub)
        except Exception as e:
            logger.warning("Cannot read user_disabled_tools for %s: %s", sub, e)
            return result
        if not disabled:
            return result
        ctx = context.fastmcp_context
        if ctx is None:
            logger.warning("fastmcp_context is None at on_initialize for sub=%s", sub)
            return result
        try:
            await disable_components(
                ctx,
                names=set(disabled),
                components={"tool"},
            )
        except Exception as e:
            logger.warning("Failed to apply disabled tools for %s: %s", sub, e)
        return result
