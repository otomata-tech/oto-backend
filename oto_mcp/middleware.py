"""Middlewares FastMCP — filtrage des tools par user."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from . import db
from .auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)


class UserDisabledToolsMiddleware(Middleware):
    """Filtre la liste des tools et bloque les appels selon les préférences
    de l'utilisateur stockées dans `user_disabled_tools`.

    - `tools/list` : retire les tools désactivés AVANT envoi au client.
    - `tools/call` : si appelé sur un tool désactivé (cas d'un client qui
      garde une liste en cache), renvoie une erreur explicite.

    Si pas de sub identifiable (request non-authentifiée, ex. listing
    initial pour discovery), pas de filtrage — comportement neutre.
    """

    def _user_disabled(self) -> set[str]:
        try:
            sub = current_user_sub_from_token()
        except Exception:
            return set()
        if not sub:
            return set()
        try:
            return set(db.list_user_disabled_tools(sub))
        except Exception as e:
            logger.warning("Cannot read user_disabled_tools for %s: %s", sub, e)
            return set()

    async def on_list_tools(self, context, call_next):
        result = await call_next(context)
        disabled = self._user_disabled()
        if not disabled:
            return result
        # `result` est une liste de Tool ; on filtre.
        try:
            return [t for t in result if getattr(t, "name", None) not in disabled]
        except TypeError:
            return result

    async def on_call_tool(self, context, call_next):
        disabled = self._user_disabled()
        if disabled:
            tool_name = getattr(context.message, "name", None)
            if tool_name and tool_name in disabled:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=(
                        f"Tool `{tool_name}` désactivé pour ton compte. "
                        f"Réactive-le sur https://app.oto.ninja/account."
                    ),
                ))
        return await call_next(context)
