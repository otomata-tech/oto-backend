"""Méta-tools — pilotage des préférences de l'user depuis la conversation.

Permet à l'assistant (Claude.ai, Claude Code) de désactiver/réactiver des
tools individuellement sans passer par l'UI /account. La persistance reste
en DB (`user_disabled_tools`), et les changements émettent immédiatement
`tools/list_changed` à la session courante grâce à `disable_components` /
`enable_components` (fastmcp).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp import Context, FastMCP
from fastmcp.server.transforms.visibility import (
    disable_components,
    enable_components,
    reset_visibility,
)
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db
from ..auth_hooks import current_user_sub_from_token
from ..tool_visibility import (
    PROTECTED_TOOLS,
    is_default_hidden,
    is_tool_visible,
)

logger = logging.getLogger(__name__)


def _require_sub() -> str:
    sub = None
    try:
        sub = current_user_sub_from_token()
    except Exception:
        pass
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Auth requise — ces tools ne marchent que sur le transport HTTP authentifié.",
        ))
    return sub


def _active_org(sub: str) -> int:
    """Org de session du sub = scope du profil de visibilité (ADR 0015/0023). 0 = perso/global.
    Toggles perso sont stockés par (sub, org_id) → on lit l'org **de session** via le seam
    unique `access.current_org` (ADR 0023 ; jamais `org_store.get_active_org` en direct, qui
    renverrait l'org maison et désynchroniserait l'UX après `oto_use_org`). ADR 0030 §6 barreau 1."""
    return access.current_org(sub) or 0


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def oto_list_my_tools(ctx: Context) -> dict:
        """List all oto-mcp tools with their enabled/disabled state for the current user.

        Returns a dict with `tools` (list of {name, enabled}) and `disabled_count`.
        """
        sub = _require_sub()
        org = _active_org(sub)
        disabled = set(db.list_user_disabled_tools(sub, org))
        enabled_override = set(db.list_user_enabled_tools(sub, org))
        # run_middleware=False : on veut la liste complète (y compris les
        # tools masqués pour ce user), sinon on n'affiche pas leur état.
        all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
        names = sorted(t.name for t in all_tools)
        states = {
            n: is_tool_visible(n, disabled, enabled_override)
            for n in names
        }
        return {
            "tools": [{"name": n, "enabled": states[n]} for n in names],
            "disabled_count": sum(1 for v in states.values() if not v),
        }

    @mcp.tool()
    async def oto_disable_tool(name: str, ctx: Context) -> dict:
        """Disable a tool for the current user — persistent across sessions.

        The tool disappears from the visible list immediately (the server
        notifies the client via tools/list_changed). Re-enable with
        `oto_enable_tool`.

        Args:
            name: Exact tool name (e.g. `attio_create_deal`, `unipile_search`).
        """
        sub = _require_sub()
        all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
        known = {t.name for t in all_tools}
        if name not in known:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{name}`. Use oto_list_my_tools to see available names.",
            ))
        if name in PROTECTED_TOOLS:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`{name}` is protected (toolset management, context switching or "
                        "usage loop) — refusing to disable.",
            ))
        org = _active_org(sub)
        db.add_user_disabled_tool(sub, name, org)
        db.remove_user_enabled_tool(sub, name, org)  # lève un éventuel override
        await disable_components(ctx, names={name}, components={"tool"})
        return {"name": name, "enabled": False, "persistent": True}

    @mcp.tool()
    async def oto_enable_tool(name: str, ctx: Context) -> dict:
        """Re-enable a previously disabled tool for the current user.

        Args:
            name: Exact tool name to re-enable.
        """
        sub = _require_sub()
        # SÉCURITÉ — visibilité-only (ADR 0031) : (dés)activer un outil = préférence
        # d'AFFICHAGE, jamais une autorisation. Rendre un outil visible ne donne PAS
        # accès à son credential. L'accès réel d'un connecteur sensible est gardé au
        # call-time, indépendamment de cette visibilité : `resolve_credential` →
        # `require_connector_access` (ADR 0025, réservation par département/membre) +
        # le cran d'activation + la résolution du credential bridge (ADR 0034). Plus
        # de garde « grant-only » ici (concept retiré : `is_grant_only` est mort).
        org = _active_org(sub)
        db.remove_user_disabled_tool(sub, name, org)
        # Override positif requis pour rendre visible un masqué-par-défaut.
        if is_default_hidden(name):
            db.add_user_enabled_tool(sub, name, org)
        await enable_components(ctx, names={name}, components={"tool"})
        return {"name": name, "enabled": True, "persistent": True}

    # --- admin : grants de namespace sensible -------------------------------

    def _require_admin() -> str:
        sub = _require_sub()
        if not access.is_super_admin(sub):
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message="Réservé au super admin.",
            ))
        return sub

    # Grants de namespace (user + org) fusionnés dans la capacité MCP
    # `oto_admin_namespace_access` (capabilities/namespace_access.py).
    #
    # Clés plateforme (list/set) RETIRÉES de la face MCP (2026-06-25) : poser une
    # clé brute = un secret en clair dans le contexte LLM → dashboard-only. CRUD
    # servi par les routes REST `/api/admin/platform-keys*` (api_routes.py).
