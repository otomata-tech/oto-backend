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


def _user_access(sub: str) -> tuple[frozenset, bool]:
    """(namespaces grantés, is_admin) pour décider la visibilité grant-only.

    `granted` = grants per-user ∪ entitlements de l'org active (source unique
    `access.granted_namespaces_for` — même calcul que le middleware et les
    gardes REST, pour qu'aucune surface ne diverge)."""
    granted = access.granted_namespaces_for(sub)
    is_admin = access.is_super_admin(sub)
    return granted, is_admin


def _active_org(sub: str) -> int:
    """Org de session du sub = scope du profil de visibilité (ADR 0015/0023). 0 = perso/global.
    Toggles/presets sont stockés par (sub, org_id) → on lit l'org **de session** via le seam
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
        granted, is_admin = _user_access(sub)
        # run_middleware=False : on veut la liste complète (y compris les
        # tools masqués pour ce user), sinon on n'affiche pas leur état.
        all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
        names = sorted(t.name for t in all_tools)
        states = {
            n: is_tool_visible(n, disabled, enabled_override, granted, is_admin)
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
        if name.startswith("oto_") and name in {"oto_enable_tool", "oto_list_my_tools"}:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`{name}` is required to manage your toolset — refusing to disable.",
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
        # le cran d'activation + `resolve_remote_credential` pour les bridges. Plus de
        # garde « grant-only » ici (concept retiré : `is_grant_only` est mort).
        org = _active_org(sub)
        db.remove_user_disabled_tool(sub, name, org)
        # Override positif requis pour rendre visible un masqué-par-défaut.
        if is_default_hidden(name):
            db.add_user_enabled_tool(sub, name, org)
        await enable_components(ctx, names={name}, components={"tool"})
        return {"name": name, "enabled": True, "persistent": True}

    # --- presets ------------------------------------------------------------

    _PROTECTED = PROTECTED_TOOLS  # source unique (tool_visibility, anti-lockout)

    async def _all_tool_names(ctx: Context) -> set[str]:
        tools = await ctx.fastmcp.list_tools(run_middleware=False)
        return {t.name for t in tools}

    @mcp.tool()
    def oto_list_presets(ctx: Context) -> dict:
        """List the current user's saved presets (named toolset snapshots).

        Each preset stores the list of tool names that should be ENABLED
        when the preset is applied. All other tools become disabled.
        """
        sub = _require_sub()
        presets = db.list_user_presets(sub, _active_org(sub))
        return {
            "presets": [
                {
                    "name": p["name"],
                    "tool_count": len(p["enabled_tools"]),
                    "updated_at": str(p["updated_at"]) if p["updated_at"] else None,
                }
                for p in presets
            ],
        }

    @mcp.tool()
    async def oto_save_preset(
        name: str,
        ctx: Context,
        enabled_tools: Optional[list[str]] = None,
    ) -> dict:
        """Save a named preset (a toolset snapshot).

        Default behavior (no `enabled_tools` passed): snapshots which tools
        are currently visible (i.e. not in user_disabled_tools).

        If `enabled_tools` is provided, saves that explicit list as the
        preset's content — without altering the user's current toolset state.
        Useful to provision a preset programmatically.

        Overwrites if the preset already exists. Apply later with
        `oto_apply_preset`.

        Args:
            name: Preset name (e.g. "sales", "mission-mm", "perso").
            enabled_tools: Optional explicit list of tool names to store.
                Names unknown to the server are rejected with INVALID_PARAMS.
        """
        sub = _require_sub()
        org = _active_org(sub)
        name = (name or "").strip()
        if not name:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Preset name required."))
        all_names = await _all_tool_names(ctx)

        if enabled_tools is not None:
            unknown = sorted(set(enabled_tools) - all_names)
            if unknown:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=f"Unknown tool names: {unknown[:5]}{'…' if len(unknown) > 5 else ''}",
                ))
            enabled = sorted(set(enabled_tools))
        else:
            disabled = set(db.list_user_disabled_tools(sub, org))
            enabled_override = set(db.list_user_enabled_tools(sub, org))
            granted, is_admin = _user_access(sub)
            enabled = sorted(
                n for n in all_names
                if is_tool_visible(n, disabled, enabled_override, granted, is_admin)
            )

        db.save_user_preset(sub, name, enabled, org)
        return {"name": name, "saved": True, "enabled_count": len(enabled)}

    @mcp.tool()
    async def oto_apply_preset(name: str, ctx: Context) -> dict:
        """Switch the user's toolset to the one defined by a saved preset.

        Replaces `user_disabled_tools` so that only the preset's enabled
        tools remain visible. Protected meta-tools (oto_list_my_tools,
        oto_enable_tool, oto_apply_preset) are always kept enabled —
        otherwise the user could lock themselves out.

        Args:
            name: Name of a previously saved preset (see oto_list_presets).
        """
        sub = _require_sub()
        org = _active_org(sub)
        preset = db.get_user_preset(sub, name, org)
        if not preset:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Preset `{name}` not found. Use oto_list_presets.",
            ))
        all_names = await _all_tool_names(ctx)
        enabled = (set(preset["enabled_tools"]) | _PROTECTED) & all_names
        disabled = sorted(all_names - enabled)

        db.replace_user_disabled_tools(sub, disabled, org)
        # Override positif pour les tools masqués-par-défaut qui en ont besoin.
        db.replace_user_enabled_tools(
            sub,
            sorted(n for n in enabled if is_default_hidden(n)),
            org,
        )

        # Reset session visibility and re-apply the new state. Notifications
        # are emitted by fastmcp inside these calls.
        await reset_visibility(ctx)
        if disabled:
            await disable_components(ctx, names=set(disabled), components={"tool"})

        return {
            "applied": name,
            "enabled_count": len(enabled),
            "disabled_count": len(disabled),
        }

    @mcp.tool()
    def oto_delete_preset(name: str, ctx: Context) -> dict:
        """Delete a saved preset by name. Does not change current toolset state."""
        sub = _require_sub()
        deleted = db.delete_user_preset(sub, name, _active_org(sub))
        return {"name": name, "deleted": deleted}

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
