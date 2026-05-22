"""Méta-tools — pilotage des préférences de l'user depuis la conversation.

Permet à l'assistant (Claude.ai, Claude Code) de désactiver/réactiver des
tools individuellement sans passer par l'UI /account. La persistance reste
en DB (`user_disabled_tools`), et les changements émettent immédiatement
`tools/list_changed` à la session courante grâce à `disable_components` /
`enable_components` (fastmcp).
"""
from __future__ import annotations

import logging

from fastmcp import Context, FastMCP
from fastmcp.server.transforms.visibility import (
    disable_components,
    enable_components,
    reset_visibility,
)
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import db
from ..auth_hooks import current_user_sub_from_token

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


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def oto_list_my_tools(ctx: Context) -> dict:
        """List all oto-mcp tools with their enabled/disabled state for the current user.

        Returns a dict with `tools` (list of {name, enabled}) and `disabled_count`.
        """
        sub = _require_sub()
        disabled = set(db.list_user_disabled_tools(sub))
        # run_middleware=False : on veut la liste complète (y compris les
        # tools désactivés pour ce user), sinon on n'affiche pas leur état.
        all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
        names = sorted(t.name for t in all_tools)
        return {
            "tools": [{"name": n, "enabled": n not in disabled} for n in names],
            "disabled_count": len(disabled),
        }

    @mcp.tool()
    async def oto_disable_tool(name: str, ctx: Context) -> dict:
        """Disable a tool for the current user — persistent across sessions.

        The tool disappears from the visible list immediately (the server
        notifies the client via tools/list_changed). Re-enable with
        `oto_enable_tool`.

        Args:
            name: Exact tool name (e.g. `attio_create_deal`, `linkedin_search`).
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
        db.add_user_disabled_tool(sub, name)
        await disable_components(ctx, names={name}, components={"tool"})
        return {"name": name, "enabled": False, "persistent": True}

    @mcp.tool()
    async def oto_enable_tool(name: str, ctx: Context) -> dict:
        """Re-enable a previously disabled tool for the current user.

        Args:
            name: Exact tool name to re-enable.
        """
        sub = _require_sub()
        db.remove_user_disabled_tool(sub, name)
        await enable_components(ctx, names={name}, components={"tool"})
        return {"name": name, "enabled": True, "persistent": True}

    # --- presets ------------------------------------------------------------

    _PROTECTED = {"oto_enable_tool", "oto_list_my_tools", "oto_apply_preset"}

    async def _all_tool_names(ctx: Context) -> set[str]:
        tools = await ctx.fastmcp.list_tools(run_middleware=False)
        return {t.name for t in tools}

    @mcp.tool()
    async def oto_list_presets(ctx: Context) -> dict:
        """List the current user's saved presets (named toolset snapshots).

        Each preset stores the list of tool names that should be ENABLED
        when the preset is applied. All other tools become disabled.
        """
        sub = _require_sub()
        presets = db.list_user_presets(sub)
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
            disabled = set(db.list_user_disabled_tools(sub))
            enabled = sorted(all_names - disabled)

        db.save_user_preset(sub, name, enabled)
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
        preset = db.get_user_preset(sub, name)
        if not preset:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Preset `{name}` not found. Use oto_list_presets.",
            ))
        all_names = await _all_tool_names(ctx)
        enabled = set(preset["enabled_tools"]) | _PROTECTED
        # Keep only enabled tools that still exist on the server.
        enabled &= all_names
        disabled = sorted(all_names - enabled)

        db.replace_user_disabled_tools(sub, disabled)

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
    async def oto_delete_preset(name: str, ctx: Context) -> dict:
        """Delete a saved preset by name. Does not change current toolset state."""
        sub = _require_sub()
        deleted = db.delete_user_preset(sub, name)
        return {"name": name, "deleted": deleted}
