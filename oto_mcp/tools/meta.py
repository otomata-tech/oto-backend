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

from .. import (access, call_axes, db, doctrine_run, providers, redaction,
                tool_alias, tool_registry)
from ..auth.hooks import current_user_sub_from_token
from ..tool_visibility import (
    PROTECTED_TOOLS,
    is_default_hidden,
    is_tool_visible,
    namespace_of,
)

# Méta/spine non dispatchables via `oto_call` (ADR 0036 §4) : déjà toujours visibles,
# aucun intérêt à passer par le dispatch, et anti-boucle (`oto_call` sur lui-même).
# Miroir de `middleware.field_redaction._SPINE_SERVICES`.
_NON_DISPATCHABLE: frozenset[str] = frozenset({"oto", "run", "feedback", "data"})

# Budget d'une ligne de catalogue. ~350 entrées rendues d'un coup : chaque caractère
# est multiplié par le nombre d'outils. 100 c. suffisent à dire ce que fait un outil ;
# le détail est dans `oto_tool_schema`, qu'on lit AVANT d'appeler de toute façon.
_CATALOG_BLURB = 100
# Recherche : borne par défaut. Au-delà, l'agent relit le catalogue entier — c'est le
# signe que la requête était trop large, pas qu'il manque des résultats.
_SEARCH_LIMIT = 40

logger = logging.getLogger(__name__)


def _namespace_help(ns: str) -> str:
    """Ligne de catalogue du connecteur d'un namespace (curée, en français) — le pont
    entre une requête en langue naturelle et des docstrings anglaises. Fail-soft."""
    try:
        con = providers.connector_for_namespace(ns)
        return f"{con.label} {con.help}" if con else ""
    # noqa: SILENT — aide de namespace absente plutôt que fausse
    except Exception:
        return ""


def _tool_prefix() -> str:
    """Le préfixe d'outils du tenant courant (`""` = noms canoniques).

    Ces cinq tools prennent un NOM en argument : ce sont les seuls endroits où un nom
    traverse un HANDLER au lieu du bord du protocole, donc les seuls que le
    `ToolAliasMiddleware` ne couvre pas. Sans ce rappel, un compte de tenant tiers
    lisait `tulina_doc` dans sa liste et se voyait répondre « Unknown tool » en le
    passant à `oto_tool_schema` — le catalogue et le dispatch auraient parlé deux
    langues."""
    try:
        return tool_alias.prefix_for(current_user_sub_from_token())
    # noqa: SILENT — dette déclarée : préfixe d'outil perdu ⇒ notre identité servie (#424, verdict C)
    except Exception:  # noqa: BLE001 — fail-open : les noms canoniques
        return ""


def _require_sub() -> str:
    sub = None
    try:
        sub = current_user_sub_from_token()
    # noqa: SILENT — dette déclarée : sub avalé (#424, verdict C — seam commun)
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
    """Objet Tool FastMCP par nom (ou None), **y compris masqué/désactivé** — on
    énumère le catalogue BRUT du `Provider` parent (« including disabled ones »,
    docstring fastmcp). ⚠️ `list_tools(run_middleware=False)` ne suffit PAS : il
    applique quand même `apply_session_transforms` + filtre `is_enabled` → un tool
    masqué par la visibilité de LA SESSION (connecteur non activé au handshake,
    non-sélectionné) était introuvable au dispatch — l'échappatoire `oto_call`/
    `oto_tool_schema` répondait « Unknown tool » (#186, régression du passage à la
    visibilité native fastmcp)."""
    from fastmcp.server.providers.base import Provider
    tools = await Provider.list_tools(ctx.fastmcp)
    for t in tools:
        if t.name == name:
            return t
    return None


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
        # noqa: SILENT — dette déclarée : la trace d'appel indirect disparaît (#424, verdict C)
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
    async def oto_list_my_tools(ctx: Context, query: Optional[str] = None,
                                limit: Optional[int] = None) -> dict:
        """The oto tool CATALOG — every tool, what it does in one line, and whether it is
        currently visible to you.

        This is the entry point of the deferred mode (`oto_list_my_tools` →
        `oto_tool_schema` → `oto_call`): the way an agent reaches oto without loading
        ~350 full schemas. Each entry carries a one-line `description` — pick from it
        rather than guessing from the name, then read the exact arguments with
        `oto_tool_schema(name)` before calling.

        Args:
            query: keywords to search the catalog (name + description + the connector's
                catalog line), e.g. "entreprises françaises", "linkedin message",
                "invoice". Ranked by how many words match, name before description.
                LEXICAL, not semantic — zero results means "rephrase, or drop the query
                and read the whole catalog", never "oto cannot do this": the response
                then carries `namespaces`, the map of every capability of the platform.
            limit: cap the number of entries returned (default: all; 40 when searching).

        Returns `{tools: [{name, description, enabled}], total, shown, disabled_count}`.
        `enabled: false` = not mounted in your session — call it anyway with `oto_call`,
        or install its connector durably with `oto_connector(op='select')`.
        """
        sub = _require_sub()
        org = _active_org(sub)
        disabled = set(db.list_user_disabled_tools(sub, org))
        enabled_override = set(db.list_user_enabled_tools(sub, org))
        # Denylist admin (org + équipe active) — même fail-open indépendant par
        # palier que session_visibility.compute_hidden_tools, pour que ce que
        # l'user VOIT ici matche ce qui est réellement monté à la session.
        admin_hidden: set[str] = set()
        try:
            admin_hidden |= access.org_admin_hidden_tools(access.current_org(sub))
        # noqa: SILENT — fail-open par palier, calqué sur compute_hidden_tools
        except Exception:
            pass
        try:
            admin_hidden |= access.group_admin_hidden_tools(access.current_group(sub))
        # noqa: SILENT — fail-open par palier, calqué sur compute_hidden_tools
        except Exception:
            pass
        # run_middleware=False : on veut la liste complète (y compris les
        # tools masqués pour ce user), sinon on n'affiche pas leur état.
        all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
        # Le catalogue annonce les noms tels que l'utilisateur les VOIT (cf.
        # `tool_alias`) ; tout ce qui se calcule — namespace, visibilité — repart du
        # nom canonique. Le retour `canonical(public(x)) == x` est total, donc aucun
        # nom ne se perd en route.
        prefix = _tool_prefix()
        entries = sorted(
            ({"name": tool_alias.public(t.name, prefix),
              "description": tool_registry.blurb(t.description, _CATALOG_BLURB),
              # Ligne de catalogue du connecteur : le seul texte FRANÇAIS de l'entrée
              # (les docstrings sont en anglais). Sert la recherche, pas la sortie.
              "namespace_help": _namespace_help(namespace_of(t.name))}
             for t in all_tools),
            key=lambda e: e["name"])
        for e in entries:
            e["enabled"] = is_tool_visible(tool_alias.canonical(e["name"], prefix),
                                           disabled, enabled_override,
                                           frozenset(admin_hidden))
        out: dict = {
            "total": len(entries),
            "disabled_count": sum(1 for e in entries if not e["enabled"]),
        }
        if query:
            entries = tool_registry.match(query, entries)
            out["query"] = query
            if not entries:
                # Zéro résultat lexical ≠ « oto ne sait pas faire » (le piège que la
                # recherche pourrait CRÉER). On rend la carte des capacités : l'agent
                # repart du domaine au lieu de conclure à une lacune.
                out["namespaces"] = providers.render_namespace_catalog()
                out["hint"] = ("Aucun outil ne porte ces mots (recherche lexicale, "
                               "docstrings en anglais). Repère le domaine dans "
                               "`namespaces`, ou relance sans `query` pour le catalogue "
                               "complet.")
        cap = limit if limit is not None else (_SEARCH_LIMIT if query else None)
        shown = entries[:cap] if cap else entries
        out["shown"] = len(shown)
        out["tools"] = [{k: e[k] for k in ("name", "description", "enabled")}
                        for e in shown]
        return out

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
        # Le nom peut arriver sous la forme du tenant (`tulina_doc`) : la denylist,
        # elle, s'écrit en canonique — sinon le même outil s'y retrouverait deux fois,
        # et le toggle ne mordrait plus après un changement de préfixe. Le retour
        # reprend la forme MONTRÉE, celle que l'agent vient de lire dans sa liste.
        prefix = _tool_prefix()
        name = tool_alias.canonical(name, prefix)
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
        return {"name": tool_alias.public(name, prefix), "enabled": False,
                "persistent": True}

    @mcp.tool()
    async def oto_enable_tool(name: str, ctx: Context) -> dict:
        """Re-enable a previously disabled tool for the current user.

        Args:
            name: Exact tool name to re-enable.
        """
        sub = _require_sub()
        prefix = _tool_prefix()
        name = tool_alias.canonical(name, prefix)
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
        return {"name": tool_alias.public(name, prefix), "enabled": True,
                "persistent": True}

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
        prefix = _tool_prefix()
        demande, name = name, tool_alias.canonical(name, prefix)
        tool = await _resolve_tool(ctx, name)
        if tool is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{demande}`. Use oto_list_my_tools to see available names."))
        return {
            # Rendu sous le nom que l'appelant VERRA dans sa liste, pas sous le nom
            # interne : il va le recopier dans `oto_call`.
            "name": tool_alias.public(name, prefix),
            "namespace": tool_alias.public_namespace(namespace_of(name), prefix),
            "description": (tool.description or "").strip(),
            "input_schema": getattr(tool, "parameters", None),
            "output_schema": getattr(tool, "output_schema", None),
        }

    @mcp.tool()
    async def oto_call(name: str, arguments: Optional[dict] = None,
                       _org: Optional[int] = None, *, ctx: Context):
        """Call ANY oto tool by name — including one that is NOT listed (hidden by
        default, connector not activated, FOD…), for a single call, WITHOUT adding it
        durably to your toolbox.

        Use this when you need a tool that does not appear in your tool list. If the
        tool IS already visible, call it directly — don't wrap it in `oto_call`.
        Discover names and schemas with `oto_list_my_tools` / `oto_tool_schema`.
        META/SPINE tools (`oto_*`, `data_*`, `run_*`, `feedback`) are NOT routable
        here — they are always visible: call them directly.

        This bypasses only the DISPLAY filter, never access control: the target's
        call-time gates (credential, connector RBAC, activation, admin autz) and the
        org field-redaction policy apply exactly as for a direct call (ADR 0036).

        Args:
            name: Exact target tool name (e.g. `fr_ccn_search`).
            arguments: Argument object passed to the target tool. `{}` if none.
            _org: run the target tool under THIS organization (id) — resolves its
                credentials/visibility/data for that org (ADR 0038 call token,
                same membership guard as the flat `_org=` axis). Omit for your
                current org.

        Call-context tokens (ADR 0038) are PREFIXED `_` — `_group`, `_project`,
        `_instance`, `_account`, `_run_id` — and may be included INSIDE `arguments`:
        they route the CALL CONTEXT (which org/team/credential-instance the target
        resolves under), are guarded exactly like on a listed tool, and are stripped
        before the target sees them. E.g. reach a team-scoped connector via
        `arguments={..., "_group": 3}`, or pin an instance via
        `"_instance": "<ref from oto_instance>"`.

        The prefix keeps them out of the tools' own argument space: an unprefixed
        `account`/`org`/`project` in `arguments` is a BUSINESS argument of the target
        (e.g. `aiark_search(op="companies", account=…)` is AI Ark's company filter) and is
        passed through untouched.
        """
        # Identité ambiante : le sub du JWT porte déjà l'appel (le handler cible
        # résout ses propres credentials dessus). Soft — sur stdio local il n'y a pas
        # de sub et tout le catalogue est déjà accessible.
        sub = None
        try:
            sub = current_user_sub_from_token()
        # noqa: SILENT — sans sub (stdio local) tout le catalogue est déjà accessible
        except Exception:
            pass

        # Le nom vient du catalogue, donc éventuellement sous la forme du tenant. Il
        # redevient canonique AVANT le gate méta/spine : sans ça `tulina_doc` résout un
        # namespace inconnu, échappe à `_NON_DISPATCHABLE`, et l'anti-boucle saute.
        demande, name = name, tool_alias.canonical(name, _tool_prefix())
        if namespace_of(name) in _NON_DISPATCHABLE:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`{demande}` est un outil méta/spine — appelle-le directement, "
                        "pas via oto_call."))

        tool = await _resolve_tool(ctx, name)
        if tool is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{demande}`. Use oto_list_my_tools to see available names."))

        args = arguments if isinstance(arguments, dict) else {}
        # Axes-contexte d'appel (ADR 0038). oto_call s'exécute HORS middleware → les
        # axes des tools plats (org/group/project/instance/account/run_id) ne sont pas
        # posés pour nous. On rejoue NOUS-MÊMES la boucle applies-gated du middleware
        # plat (`call_axes.axes_for_call` — les axes LUS, pas les seuls ANNONCÉS :
        # `_account=` est accepté sur tout connecteur multi-compte même quand le
        # schéma ne l'advertise pas, sinon `strip_unconsumed_axes` l'avale et l'appel
        # part sur le compte par défaut, sans erreur — review #399 F1 ;
        # ordre AXES → le plus spécifique co-pose son org) :
        # chaque axe présent dans `arguments` (ou le param top-level `_org=`, folded
        # ci-dessous) est GARDÉ+POSÉ puis RETIRÉ des args. Posé AVANT le try de run pour
        # qu'un refus de garde PROPAGE (McpError) au lieu d'être capturé comme une erreur
        # de la cible. Ferme #228 (instance/groupe d'un connecteur injoignable via
        # oto_call — seul `_org=` était honoré).
        if _org is not None:
            args.setdefault("_org", _org)
        call_axes.reject_legacy_axis_names(args, getattr(tool, "parameters", None))
        undo: list = []
        try:
            for axis in call_axes.axes_for_call(name):
                if axis.param in args:
                    undo.extend(await axis.pin_for(args.pop(axis.param), name))
        except BaseException:
            for _reset, _tok in reversed(undo):
                _reset(_tok)
            raise
        # Un jeton passé pour un tool qui ne le supporte PAS (ex. `_instance` sur data_*,
        # `_org` sur un tool non org-scopable) = contexte sans effet → écarté des args,
        # pour ne pas casser sa validation. Sûr parce que les jetons sont préfixés `_` :
        # un argument MÉTIER homonyme (aiark `account` = le filtre société) ne porte pas
        # le préfixe et n'est donc jamais touché (issue #250).
        call_axes.strip_unconsumed_axes(args)
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
                message=f"Arguments invalides pour `{demande}` — voir `input_schema`.",
                data={"input_schema": getattr(tool, "parameters", None),
                      "errors": e.errors()}))
        # noqa: SILENT — l'échec de l'outil appelé est rendu dans ok/err au demandeur
        except Exception as e:  # noqa: BLE001 — l'erreur de la cible EST un résultat
            ok, err = False, str(e)
            # `tool` reprend le nom DEMANDÉ : l'agent le relit pour réessayer, et un
            # nom qu'il n'a jamais tapé le ferait douter de sa propre requête. Le
            # journal, lui, écrit le canonique (`_trace_target_call` juste dessous).
            return {"tool": demande, "ok": False, "error": str(e)}
        finally:
            for _reset, _tok in reversed(undo):
                _reset(_tok)
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
    # servi par les routes REST `/api/admin/platform-keys*` (api/routes.py).
