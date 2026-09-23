"""`UserDisabledToolsMiddleware` — la visibilité des tools du user, par session."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware

from .. import run_org
from ..auth.hooks import current_user_sub_from_token
from ..session_visibility import apply_session_visibility

logger = logging.getLogger(__name__)


class UserDisabledToolsMiddleware(Middleware):
    """Applique la visibilité des tools du user à sa session MCP.

    Au handshake `initialize`, pour le `sub` JWT courant, on calcule l'ensemble
    effectif des tools à masquer = `user_disabled_tools` ∪ (masqués par défaut non
    activés) ∪ (connecteurs non activés/en pause) ∪ (gates admin/alpha) et on pose
    une visibility rule session-scopée. Le calcul + l'application vivent dans
    `session_visibility` (partagés avec le refresh à chaud post-`oto_use_org`,
    ADR 0009/0011/0015). fastmcp gère nativement filtrage `tools/list`, blocage
    `tools/call` et émission de `tools/list_changed`.

    Pas de sub identifiable (stdio local, discovery non-authentifié) → on ne filtre
    rien : la machine du dev a accès complet, le masquage par défaut ne concerne que
    la surface multi-user authentifiée.

    Une session ouverte avec l'en-tête `X-Oto-Run` (le runner de flotte, un run par
    session, cf. `run_org`) voit la boîte de l'ORG DU RUN, pas la maison mutable du
    compte porteur (#1058) — sans cet en-tête, comportement inchangé.
    """

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        try:
            sub = current_user_sub_from_token()
        # noqa: SILENT — dette déclarée : sub avalé, la requête devient anonyme sans dire pourquoi (#424, verdict C)
        except Exception:
            sub = None
        if not sub:
            return result
        ctx = context.fastmcp_context
        if ctx is None:
            logger.warning("fastmcp_context is None at on_initialize for sub=%s", sub)
            return result
        org = await run_org.resolve_visibility_org(sub)
        kwargs = {} if org is None else {"org": org}
        await apply_session_visibility(ctx, sub, **kwargs)
        return result
