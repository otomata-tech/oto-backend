"""Méta-tools — pilotage des préférences de l'user depuis la conversation.

Permet à l'assistant (Claude.ai, Claude Code) de désactiver/réactiver des
tools individuellement sans passer par l'UI /account. La persistance reste
en DB (`user_disabled_tools`), et les changements émettent immédiatement
`tools/list_changed` à la session courante grâce à `disable_components` /
`enable_components` (fastmcp).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastmcp import Context, FastMCP
from fastmcp.server.transforms.visibility import (
    disable_components,
    enable_components,
    reset_visibility,
)
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from pydantic import ValidationError

from .. import access, db, doctrine_run, redaction
from ..auth_hooks import current_user_sub_from_token
from ..tool_visibility import (
    PROTECTED_TOOLS,
    is_default_hidden,
    is_tool_visible,
    namespace_of,
)

# Méta/spine non dispatchables via `oto_call` (ADR 0036 §4) : déjà toujours visibles,
# aucun intérêt à passer par le dispatch, et anti-boucle (`oto_call` sur lui-même).
# Miroir de `middleware._SPINE_SERVICES`.
_NON_DISPATCHABLE: frozenset[str] = frozenset({"oto", "run", "feedback", "data"})

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


async def _resolve_tool(ctx: Context, name: str):
    """Objet Tool FastMCP par nom (ou None), **y compris masqué** : `run_middleware=
    False` liste le catalogue COMPLET — la denylist de visibilité ne filtre que le
    listing exposé au client, pas cette énumération interne (ADR 0031/0036)."""
    tools = await ctx.fastmcp.list_tools(run_middleware=False)
    for t in tools:
        if t.name == name:
            return t
    return None


def _enforce_alpha_gate(sub: Optional[str], name: str) -> None:
    """Rejoue le gate alpha (ADR 0013) sur la cible d'`oto_call` : un compte non
    'active' ne peut dispatcher que l'allowlist d'onboarding. No-op si le flag est
    off, pas de sub, ou cible allowlistée. Fail-OPEN sur glitch DB (comme
    `session_visibility`)."""
    from ..session_visibility import ALPHA_GATE_ALLOWLIST, alpha_gate_enabled
    if not sub or not alpha_gate_enabled() or name in ALPHA_GATE_ALLOWLIST:
        return
    try:
        status = (db.get_user(sub) or {}).get("access_status")
    except Exception:
        status = "active"
    if status not in (None, "active"):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Compte en attente d'activation — `{name}` indisponible via oto_call."))


async def _trace_target_call(sub: Optional[str], name: str, args: dict, ok: bool,
                             error: Optional[str], duration_ms: int) -> None:
    """Journalise l'appel dispatché SOUS LE NOM CIBLE (ADR 0036 §5 / 0017) : sans ça
    seul `oto_call` apparaît dans `tool_calls` et l'inventaire d'usage devient aveugle
    au catalogue latent. Best-effort — jamais bloquant."""
    try:
        session_id, run_id = None, None
        try:
            from fastmcp.server.dependencies import get_context
            c = get_context()
            session_id = c.session_id
            run_id = await doctrine_run.active_run_id(c)
        except Exception:
            pass
        row = {
            "server": "oto", "kind": "mcp", "sub": sub, "tool": name,
            "args": args, "ok": ok, "error": error, "duration_ms": duration_ms,
            "session_id": session_id, "run_id": run_id,
            "org_id": access.current_org(sub),
        }
        await asyncio.to_thread(db.insert_tool_call, row)
    except Exception:
        logger.warning("traçage oto_call → %s échoué (non bloquant)", name, exc_info=True)


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

    # --- dispatch universel (ADR 0036) --------------------------------------

    @mcp.tool()
    async def oto_tool_schema(name: str, ctx: Context) -> dict:
        """Return the input JSON Schema of ANY oto tool by name — even one that is
        NOT currently listed (hidden by default, connector not activated, FOD…).

        Use this to learn the exact `arguments` shape before calling a latent tool
        with `oto_call`. Tool names come from `oto_list_my_tools`.

        Args:
            name: Exact tool name (e.g. `fr_ccn_search`, `foncier_dpe_adresse`).
        """
        _require_sub()
        tool = await _resolve_tool(ctx, name)
        if tool is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{name}`. Use oto_list_my_tools to see available names."))
        return {
            "name": name,
            "namespace": namespace_of(name),
            "description": (tool.description or "").strip(),
            "input_schema": getattr(tool, "parameters", None),
            "output_schema": getattr(tool, "output_schema", None),
        }

    @mcp.tool()
    async def oto_call(name: str, arguments: Optional[dict] = None, *, ctx: Context):
        """Call ANY oto tool by name — including one that is NOT listed (hidden by
        default, connector not activated, FOD…), for a single call, WITHOUT adding it
        durably to your toolbox.

        Use this when you need a tool that does not appear in your tool list. If the
        tool IS already visible, call it directly — don't wrap it in `oto_call`.
        Discover names and schemas with `oto_list_my_tools` / `oto_tool_schema`.

        This bypasses only the DISPLAY filter, never access control: the target's
        call-time gates (credential, connector RBAC, activation, admin autz) and the
        org field-redaction policy apply exactly as for a direct call (ADR 0036).

        Args:
            name: Exact target tool name (e.g. `fr_ccn_search`).
            arguments: Argument object passed to the target tool. `{}` if none.
        """
        # Identité ambiante : le sub du JWT porte déjà l'appel (le handler cible
        # résout ses propres credentials dessus). Soft — sur stdio local il n'y a pas
        # de sub et tout le catalogue est déjà accessible.
        sub = None
        try:
            sub = current_user_sub_from_token()
        except Exception:
            pass

        if namespace_of(name) in _NON_DISPATCHABLE:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`{name}` est un outil méta/spine — appelle-le directement, "
                        "pas via oto_call."))

        # Gate alpha (ADR 0013) — SEUL gate sans backstop call-time (fail-open
        # visibilité pure) : oto_call doit le rejouer, sinon un compte waitlisté
        # atteindrait tout le catalogue via le dispatch.
        _enforce_alpha_gate(sub, name)

        tool = await _resolve_tool(ctx, name)
        if tool is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{name}`. Use oto_list_my_tools to see available names."))

        args = arguments if isinstance(arguments, dict) else {}
        started = time.monotonic()
        ok, err = True, None
        try:
            # `Tool.run` : injection de `ctx`, validation du schéma, exécution — mais
            # HORS chaîne de middleware (donc hors rédaction) : on la ré-applique plus
            # bas. C'est ce qui permet d'atteindre un outil masqué (la denylist de
            # visibilité ne bloque que le chemin protocole `tools/call`).
            result = await tool.run(args)
        except ValidationError as e:
            ok, err = False, "invalid_arguments"
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Arguments invalides pour `{name}` — voir `input_schema`.",
                data={"input_schema": getattr(tool, "parameters", None),
                      "errors": e.errors()}))
        except Exception as e:  # noqa: BLE001 — l'erreur de la cible EST un résultat
            ok, err = False, str(e)
            return {"tool": name, "ok": False, "error": str(e)}
        finally:
            await _trace_target_call(sub, name, args, ok, err,
                                     int((time.monotonic() - started) * 1000))

        # Rédaction ré-appliquée (ADR 0036 §2) via la logique PARTAGÉE fail-closed —
        # sinon un connecteur à PII surfacé par oto_call fuiterait (le middleware a vu
        # le service « oto », pas le namespace cible).
        service = namespace_of(name)
        payload = redaction.extract_payload(result)
        try:
            red = redaction.redact_payload(service, payload)
        except redaction.RedactionWithheld:
            return redaction.withheld_result(name)
        return result if red is redaction.PASSTHROUGH else redaction.rebuild_result(result, red)

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
