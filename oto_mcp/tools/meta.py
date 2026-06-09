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
    ADMIN_GRANT_ONLY_NAMESPACES,
    DEFAULT_HIDDEN_TOOLS,
    is_entitled,
    is_grant_only,
    is_tool_visible,
    namespace_of,
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
    is_admin = access.get_user_role(sub) == access.ADMIN
    return granted, is_admin


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def oto_list_my_tools(ctx: Context) -> dict:
        """List all oto-mcp tools with their enabled/disabled state for the current user.

        Returns a dict with `tools` (list of {name, enabled}) and `disabled_count`.
        """
        sub = _require_sub()
        disabled = set(db.list_user_disabled_tools(sub))
        enabled_override = set(db.list_user_enabled_tools(sub))
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
        db.remove_user_enabled_tool(sub, name)  # lève un éventuel override
        await disable_components(ctx, names={name}, components={"tool"})
        return {"name": name, "enabled": False, "persistent": True}

    @mcp.tool()
    async def oto_enable_tool(name: str, ctx: Context) -> dict:
        """Re-enable a previously disabled tool for the current user.

        Tools in an admin-grant-only namespace (e.g. `gocardless_*`, `mm_*`)
        cannot be self-enabled by a non-admin: an admin must grant the namespace
        first (`oto_admin_grant_namespace`). The call is refused otherwise.

        Args:
            name: Exact tool name to re-enable.
        """
        sub = _require_sub()
        granted, is_admin = _user_access(sub)
        if is_grant_only(name) and not is_admin and namespace_of(name) not in granted:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"`{name}` relève du namespace contrôlé `{namespace_of(name)}` "
                    f"(accès sensible). Auto-activation refusée — demande à un admin "
                    f"de t'accorder ce namespace (oto_admin_grant_namespace)."
                ),
            ))
        db.remove_user_disabled_tool(sub, name)
        # Override positif requis pour rendre visible un masqué-par-défaut, ou un
        # grant-only côté admin (côté user granté, le grant suffit à le révéler).
        if name in DEFAULT_HIDDEN_TOOLS or (is_grant_only(name) and is_admin):
            db.add_user_enabled_tool(sub, name)
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
            enabled_override = set(db.list_user_enabled_tools(sub))
            granted, is_admin = _user_access(sub)
            enabled = sorted(
                n for n in all_names
                if is_tool_visible(n, disabled, enabled_override, granted, is_admin)
            )

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
        granted, is_admin = _user_access(sub)
        requested = (set(preset["enabled_tools"]) | _PROTECTED) & all_names
        # Un preset ne peut pas révéler un grant-only non autorisé (anti-escalade).
        enabled = {n for n in requested if is_entitled(n, granted, is_admin)}
        disabled = sorted(all_names - enabled)

        db.replace_user_disabled_tools(sub, disabled)
        # Override positif pour les tools qui en ont besoin pour être visibles :
        # masqués-par-défaut, et grant-only côté admin.
        db.replace_user_enabled_tools(
            sub,
            sorted(
                n for n in enabled
                if n in DEFAULT_HIDDEN_TOOLS or (is_grant_only(n) and is_admin)
            ),
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
    async def oto_delete_preset(name: str, ctx: Context) -> dict:
        """Delete a saved preset by name. Does not change current toolset state."""
        sub = _require_sub()
        deleted = db.delete_user_preset(sub, name)
        return {"name": name, "deleted": deleted}

    # --- admin : grants de namespace sensible -------------------------------

    def _require_admin() -> str:
        sub = _require_sub()
        if access.get_user_role(sub) != access.ADMIN:
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message="Réservé aux admins.",
            ))
        return sub

    def _resolve_target_sub(target: str) -> str:
        if "@" in target:
            user = db.get_user_by_email(target)
            if not user:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=f"Aucun user connu avec l'email `{target}`.",
                ))
            return user["sub"]
        return target

    def _check_grant_namespace(namespace: str) -> None:
        if namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"`{namespace}` n'est pas un namespace contrôlé. "
                    f"Contrôlés : {sorted(ADMIN_GRANT_ONLY_NAMESPACES)}."
                ),
            ))

    @mcp.tool()
    async def oto_admin_grant_namespace(target: str, namespace: str, ctx: Context) -> dict:
        """[admin] Accorde à un user l'accès à un namespace sensible (grant-only).

        Args:
            target: `sub` Logto, ou email du user destinataire.
            namespace: namespace contrôlé (`mm`, `gocardless`).
        """
        admin_sub = _require_admin()
        _check_grant_namespace(namespace)
        target_sub = _resolve_target_sub(target)
        db.grant_namespace(target_sub, namespace, granted_by=admin_sub)
        return {"granted": namespace, "to": target_sub}

    @mcp.tool()
    async def oto_admin_revoke_namespace(target: str, namespace: str, ctx: Context) -> dict:
        """[admin] Révoque l'accès d'un user à un namespace sensible."""
        _require_admin()
        target_sub = _resolve_target_sub(target)
        existed = db.revoke_namespace(target_sub, namespace)
        return {"revoked": namespace, "from": target_sub, "existed": existed}

    @mcp.tool()
    async def oto_admin_list_namespace_grants(
        ctx: Context, namespace: Optional[str] = None
    ) -> dict:
        """[admin] Liste les grants de namespace (tous, ou filtrés par namespace)."""
        _require_admin()
        return {"grants": db.list_namespace_grants(namespace)}

    @mcp.tool()
    async def oto_admin_list_platform_keys(ctx: Context) -> dict:
        """[admin] Liste les clés plateforme (coffre DB) — provider, label, id.

        Jamais la valeur du secret. La DB est la seule source des platform keys
        (plus de bootstrap SOPS au boot) : ce qui est listé ici est exactement
        ce que `resolve_api_key` peut servir via un grant.
        """
        _require_admin()
        keys = [
            {"id": k["id"], "provider": k["provider"], "label": k["label"]}
            for k in db.list_platform_keys()
        ]
        return {"platform_keys": keys}

    @mcp.tool()
    async def oto_admin_set_platform_key(
        provider: str, api_key: str, ctx: Context, label: str = "env"
    ) -> dict:
        """[admin] Pose ou rote une clé plateforme dans le coffre DB.

        Remplace l'ancien import SOPS au boot : c'est LE chemin pour provisionner
        ou roter une clé partagée (modèle : user key OU platform key + grant +
        quota). Poser une clé ne la grante à personne.

        Args:
            provider: provider keyé du registre (serper, hunter, sirene, attio,
                lemlist, kaspr, pennylane, slack, fullenrich).
            api_key: la nouvelle valeur (rotation = re-poser sur le même
                provider+label).
            label: étiquette de la clé (défaut `env`, le label historique servi
                par resolve_api_key).
        """
        _require_admin()
        if provider not in db.KEY_PROVIDERS:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"`{provider}` n'est pas un provider keyé. "
                    f"Valides : {sorted(db.KEY_PROVIDERS)}."
                ),
            ))
        if not api_key.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="api_key vide."))
        key_id = db.upsert_platform_key(provider, label.strip() or "env", api_key.strip())
        return {"id": key_id, "provider": provider, "label": label.strip() or "env", "set": True}
