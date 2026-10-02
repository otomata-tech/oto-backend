"""Méta-tools du mode différé — atteindre un outil sans charger son schéma.

`oto_tool_schema` rend le schéma d'entrée d'un outil par son nom, `oto_call` l'exécute
(dispatch universel, ADR 0036). Ils sont MCP-only par nature : ils agissent sur
l'instance FastMCP elle-même. La toolbox du membre (catalogue, masquer/démasquer un
outil) n'est plus ici : ce sont des capacités, `capabilities/tools_me.py` (#429).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from fastmcp import Context, FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .. import (access, call_axes, calllog, db, deprecations, error_taxonomy, guide_run,
                outils_retires, redaction, run_org, session_org, tool_alias)
from ..auth.hooks import current_user_sub_from_token
from ..connectors import activation_gate
from ..connectors import health as connector_health
from ..tool_visibility import namespace_of

# Méta/spine non dispatchables via `oto_call` (ADR 0036 §4) : déjà toujours visibles,
# aucun intérêt à passer par le dispatch, et anti-boucle (`oto_call` sur lui-même).
# Miroir de `middleware.field_redaction._SPINE_SERVICES`.
_NON_DISPATCHABLE: frozenset[str] = frozenset({"oto", "run", "feedback", "data"})


def _refuser_si_retire(name: str) -> None:
    """Un nom RETIRÉ (`outils_retires`) refuse ici avec le MÊME texte que l'appel direct :
    il nomme le geste qui aboutit. `name` est déjà canonique (préfixe de tenant levé)."""
    retire = outils_retires.retrait(name)
    if retire is not None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=retire.message))

logger = logging.getLogger(__name__)


def _tool_prefix() -> str:
    """Le préfixe d'outils du tenant courant (`""` = noms canoniques).

    Ces tools prennent un NOM en argument (comme `oto_disable_tool`/`oto_enable_tool`,
    `capabilities/tools_me.py`) : ce sont les seuls endroits où un nom traverse un
    HANDLER au lieu du bord du protocole, donc les seuls que le `ToolAliasMiddleware`
    ne couvre pas. Sans ce rappel, un compte de tenant tiers
    lisait `acme_doc` dans sa liste et se voyait répondre « Unknown tool » en le
    passant à `oto_tool_schema` — le catalogue et le dispatch auraient parlé deux
    langues."""
    # `prefix_for` ne lève pas (registre en mémoire, fail-open journalisé chez lui) :
    # seul l'échec d'IDENTITÉ pouvait tomber ici, et il monte — servir nos noms
    # canoniques à un compte dont on ignore l'identité, c'était le taire (#464).
    return tool_alias.prefix_for(current_user_sub_from_token())


def _require_sub() -> str:
    # Un échec d'identité MONTE (le seam le journalise avec sa raison, #464) : seul
    # un appel réellement sans jeton est « non authentifié ».
    sub = current_user_sub_from_token()
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Auth requise — ces tools ne marchent que sur le transport HTTP authentifié.",
        ))
    return sub


async def resoudre_outil(fastmcp, name: str):
    """Objet Tool FastMCP par nom (ou None), **y compris masqué/désactivé** — on
    énumère le catalogue BRUT du `Provider` parent (« including disabled ones »,
    docstring fastmcp). ⚠️ `list_tools(run_middleware=False)` ne suffit PAS : il
    applique quand même `apply_session_transforms` + filtre `is_enabled` → un tool
    masqué par la visibilité de LA SESSION (connecteur non activé au handshake,
    non-sélectionné) était introuvable au dispatch — l'échappatoire `oto_call`/
    `oto_tool_schema` répondait « Unknown tool » (#186, régression du passage à la
    visibilité native fastmcp)."""
    from fastmcp.server.providers.base import Provider
    tools = await Provider.list_tools(fastmcp)
    for t in tools:
        if t.name == name:
            return t
    return None


# Les clés du relevé qui FACTURENT : elles vont sur la ligne de la cible, et sur elle
# seule. La ligne d'enveloppe (`tool='oto_call'`) ne doit pas les porter — une même
# consommation écrite deux fois serait facturée deux fois le jour où un consommateur
# ne filtrerait plus par nom d'outil.
# Les compteurs `found_*` (contacts d'un job FullEnrich où une valeur de chaque sorte
# a été trouvée) FACTURENT aussi : ce sont eux qui portent le prix par résultat.
_BILLING_TRACE_KEYS = ("quantity", "key_mode",
                       "found_work_emails", "found_personal_emails", "found_phones")
_UNSET = object()


async def _trace_target_call(sub: Optional[str], name: str, args: dict, ok: bool,
                             error: Optional[str], duration_ms: int, *,
                             trace: Optional[dict] = None,
                             org_id: object = _UNSET,
                             run_id: Optional[str] = None) -> None:
    """Journalise l'appel dispatché SOUS LE NOM CIBLE (ADR 0036 §5 / 0017) : sans ça
    seul `oto_call` apparaît dans `tool_calls` et l'inventaire d'usage devient aveugle
    au catalogue latent. Best-effort — jamais bloquant.

    ⚠️ **Même fabrique d'arguments que le middleware** (`calllog.truncated_args`) : cette
    ligne-ci est servie par les MÊMES surfaces (fiche d'un appel, timeline d'un déroulé),
    qui annoncent toutes deux des arguments tronqués et masqués. Elle posait le
    dictionnaire brut jusqu'au 2026-09-01 — 40 159 lignes en base, dont les 111 seules
    dont une valeur dépassait la borne annoncée. Le nom CIBLE est celui qui déclare ses
    secrets : c'est lui qu'on passe, jamais `oto_call`."""
    try:
        session_id = None
        try:
            from fastmcp.server.dependencies import get_context
            c = get_context()
            session_id = c.session_id
            # Même source que le sink du middleware : le jeton `_run_id=` d'abord (lu
            # par l'appelant AVANT le reset des axes), la pile de session ensuite.
            run_id = run_id or await guide_run.active_run_id(c)
        # noqa: SILENT — dette déclarée : la trace d'appel indirect disparaît (#424, verdict C)
        except Exception:
            pass
        row = {
            "server": "oto", "kind": "mcp", "sub": sub, "tool": name,
            "args": calllog.truncated_args(args, tool=name),
            "ok": ok, "error": error, "duration_ms": duration_ms,
            "session_id": session_id, "run_id": run_id,
            # L'org SOUS LAQUELLE LA CIBLE A RÉSOLU, lue par `oto_call` avant de défaire
            # ses axes — pas l'org maison qu'on relirait après coup.
            "org_id": access.current_org(sub) if org_id is _UNSET else org_id,
        }
        # L'émetteur déclaré (client + jeton nommé), même règle que la ligne
        # d'enveloppe : sans elle, la cible d'un dispatch n'aurait pas d'émetteur.
        calllog.poser_emetteur(row)
        # La même règle que le sink du middleware : sans elle, la cible d'un dispatch
        # n'avait ni `key_mode` ni `quantity`, donc n'était jamais facturée.
        # La liste fermée des clés versées dans `args` vit dans `server` (import au
        # moment de l'appel : le module est déjà chargé en production, et l'outil ne
        # doit pas en recopier une seconde version).
        from .. import server as _server
        calllog.apply_call_trace(row, trace, _server._TRACED_ARGS)
        await asyncio.to_thread(db.insert_tool_call, row)
    except Exception:
        logger.warning("traçage oto_call → %s échoué (non bloquant)", name, exc_info=True)


async def _resolve_tool(ctx: Context, name: str):
    return await resoudre_outil(ctx.fastmcp, name)


@dataclass
class IssueCible:
    """Ce qu'a rendu un outil exécuté par `executer_cible`.

    `ok` : le résultat SERVI (rédaction appliquée), tel que l'appel direct l'aurait
    rendu. Sinon `message` est le texte SCRUBBÉ (`error_taxonomy`), `code` sa classe
    (`quota_exhausted`, `upstream_timeout`…) et `retryable` ce qu'elle en dit — de quoi
    décider sans relire le texte."""
    ok: bool
    result: object = None
    message: Optional[str] = None
    code: Optional[str] = None
    retryable: bool = False
    # La politique de rédaction de l'org a RETENU le résultat entier : `result` est le
    # message de retenue, pas une donnée. Une recette refuse la page plutôt que d'écrire.
    retenu: bool = False


async def executer_cible(tool, sub: Optional[str], name: str, demande: str,
                         args: dict) -> IssueCible:
    """Exécute l'outil `name` (CANONIQUE, déjà jugé dispatchable) comme `oto_call` :
    mêmes gardes, même journal, même rédaction. Partagé par `oto_call` et les recettes
    (`recipes/moteur.py`), qui appellent des outils depuis le serveur, hors protocole.

    Lève `McpError` sur un outil inconnu, des arguments invalides ou un refus de garde
    (activation, appartenance d'un axe) : ce sont des fautes de l'APPELANT. L'échec de
    la CIBLE, lui, est un résultat : `IssueCible(ok=False, …)`.

    `tool` est l'objet déjà résolu (`resoudre_outil`) : l'appelant dit lui-même
    « inconnu » avec ses mots."""
    call_axes.reject_legacy_axis_names(args, getattr(tool, "parameters", None))
    undo: list = []
    try:
        # Hors boucle : la liste des axes lit la base (`docs/event-loop-perf.md`).
        for axis in await run_in_threadpool(call_axes.axes_for_call, name):
            if axis.param in args:
                undo.extend(await axis.pin_for(args.pop(axis.param), name))
        # L'org du RUN (#639), après les axes — même règle que le middleware :
        # sans `_org=`, la cible se résout dans l'org du run, appartenance gardée.
        undo.extend(await run_org.pin_for_call())
        # L'activation du connecteur de la CIBLE, contre l'org et l'équipe que les
        # axes viennent de poser — même garde, même place, que le middleware de
        # contexte pour un appel direct (#1064). Ici plutôt qu'à la visibilité :
        # celle-ci est un filtre d'affichage, que `oto_call` traverse par construction.
        await activation_gate.require_active(name)
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
    # Relevé PROPRE à la cible. Sans lui, ce que la cible consigne (`key_mode` au
    # résolveur, `quantity` au point où N est connu) tombait dans le relevé de la
    # requête ENVELOPPE, donc sur la ligne `tool='oto_call'` — que la lentille de
    # facturation, qui filtre par nom d'outil, ne lit jamais. Holder MUTABLE posé
    # avant `tool.run` : un handler sync tourne en threadpool sur une copie du
    # contexte, et c'est la mutation de CE dict qui remonte.
    outer_trace = session_org.current_call_trace()
    target_trace: dict = {}
    trace_tok = session_org.set_call_trace(target_trace)
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
        # Deux publics, deux messages. Le JOURNAL garde le brut, tronqué — même
        # convention que `calllog.py` (`str(e)[:MAX_ERROR_CHARS]`) pour un appel
        # normal : c'est la trace d'exploitation, elle sert à déboguer. L'AGENT,
        # lui, ne doit voir que le message SCRUBBÉ : hors chaîne de middleware
        # (cf. plus haut), `ErrorEnvelopeMiddleware` ne nettoie pas ce chemin, donc
        # on rejoue sa classification ici — sinon une exception brute (chemin
        # interne, fragment d'URL amont, id technique) remontait telle quelle à
        # l'agent, alors que le même outil appelé normalement voit son message
        # scrubbé (oto-backend#566).
        ok, err = False, str(e)[:calllog.MAX_ERROR_CHARS]
        info = error_taxonomy.classify(e)
        message = info.message
        # Même suivi de santé que l'enveloppe (`ErrorEnvelopeMiddleware`), que ce
        # chemin hors chaîne ne traverse pas : la clé servie à la CIBLE est marquée.
        if info.code == "quota_exhausted":
            await connector_health.suivre_appel(target_trace, message)
        return IssueCible(ok=False, message=message, code=info.code,
                          retryable=bool(getattr(info, "retryable", False)))
    finally:
        # Org et run de la CIBLE, lus AVANT de défaire les axes : après le reset,
        # `current_org` rend l'org maison de l'appelant, pas celle où la cible a
        # résolu ses credentials — la ligne partait sous la mauvaise org.
        target_org: object = _UNSET
        try:
            target_org = await run_in_threadpool(access.current_org, sub)
        # noqa: SILENT — best-effort : `_trace_target_call` retombe sur sa propre lecture
        except Exception:
            pass
        target_run = session_org.current_call_run()
        session_org.reset_call_trace(trace_tok)
        # L'écho rendu à l'agent (`resolved_account`/`resolved_connector`, lus par
        # `CallContextMiddleware` dans le relevé ENVELOPPE) doit survivre ; seules
        # les clés qui facturent restent sur la ligne de la cible.
        # `credential_row` reste aussi à la cible : remontée dans le relevé
        # enveloppe, elle ferait EFFACER par l'enveloppe (qui voit `oto_call`
        # réussir) la marque « crédits épuisés » que la cible vient de poser.
        if outer_trace is not None:
            outer_trace.update({k: v for k, v in target_trace.items()
                                if k not in _BILLING_TRACE_KEYS
                                and k != "credential_row"})
        for _reset, _tok in reversed(undo):
            _reset(_tok)
        await _trace_target_call(sub, name, args, ok, err,
                                 int((time.monotonic() - started) * 1000),
                                 trace=target_trace, org_id=target_org,
                                 run_id=target_run)

    # Succès de la cible : une marque « crédits épuisés » de SA clé est levée.
    await connector_health.suivre_appel(target_trace, None)

    # Rédaction ré-appliquée (ADR 0036 §2) via la logique PARTAGÉE fail-closed —
    # sinon un connecteur à PII surfacé par oto_call fuiterait (le middleware a vu
    # le service « oto », pas le namespace cible).
    service = namespace_of(name)
    payload = redaction.extract_payload(result)
    try:
        red = await run_in_threadpool(redaction.redact_payload, service, payload)
    except redaction.RedactionWithheld:
        return IssueCible(ok=True, result=redaction.withheld_result(name), retenu=True)
    return IssueCible(ok=True, result=(result if red is redaction.PASSTHROUGH
                                       else redaction.rebuild_result(result, red)))


def register(mcp: FastMCP) -> None:
    # --- dispatch universel (ADR 0036) --------------------------------------

    @mcp.tool()
    async def oto_tool_schema(name: str, ctx: Context) -> dict:
        """Return the input JSON Schema of ANY oto tool by name — even one that is
        NOT currently listed (hidden by default, connector not installed, FOD…).

        Use this to learn the exact `arguments` shape before calling a latent tool
        with `oto_call`. Tool names come from `oto_list_my_tools`.

        Args:
            name: Exact tool name (e.g. `fr_ccn_search`, `foncier_dpe_adresse`).
        """
        _require_sub()
        prefix = _tool_prefix()
        demande, name = name, deprecations.tool_canonique(
            tool_alias.canonical(name, prefix))
        _refuser_si_retire(name)
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
                       _org: Optional[int] = None, _run_id: Optional[str] = None,
                       *, ctx: Context):
        """Call ANY oto tool by name — including one that is NOT listed (hidden by
        default, connector not installed, FOD…), for a single call, WITHOUT adding it
        durably to your toolbox.

        Use this when you need a tool that does not appear in your tool list. If the
        tool IS already visible, call it directly — don't wrap it in `oto_call`.
        Discover names and schemas with `oto_list_my_tools` / `oto_tool_schema`.
        META/SPINE tools (`oto_*`, `data_*`, `run_*`, `feedback`) are NOT routable
        here — they are always visible: call them directly.

        This bypasses only the DISPLAY filter, never access control: the target's
        call-time gates (connector activation for the org/team the call resolves
        under — refused `connector_disabled` —, credential, connector RBAC, admin
        authz) and the org field-redaction policy apply exactly as for a direct call
        (ADR 0036).

        Call-context tokens (ADR 0038) are PREFIXED `_` — `_group`, `_project`,
        `_instance`, `_account`, `_run_id` — and may be included INSIDE `arguments`:
        they route the CALL CONTEXT (which org/team/credential-instance the target
        resolves under), are guarded exactly like on a listed tool, and are stripped
        before the target sees them. E.g. reach a team-scoped connector via
        `arguments={..., "_group": 3}`, or pin an instance via
        `"_instance": "<ref from oto_instance>"`.

        The prefix keeps them out of the tools' own argument space: an unprefixed
        `account`/`org`/`project` in `arguments` is a BUSINESS argument of the target
        (e.g. `aiark_company_search(account=…)` is AI Ark's company filter) and is
        passed through untouched.

        Args:
            name: Exact target tool name (e.g. `fr_ccn_search`).
            arguments: Argument object passed to the target tool. `{}` if none.
            _org: run the target tool under THIS organization (id) — resolves its
                credentials/visibility/data for that org (ADR 0038 call token,
                same membership guard as the flat `_org=` axis). Omit for your
                current org.
            _run_id: correlate this call to an open run, exactly like `_run_id` on a
                listed tool. Accepted here as well as inside `arguments` — same
                token, same effect — so the instruction « pass it on every call »
                never costs a call.
        """
        # Identité ambiante : le sub du JWT porte déjà l'appel (le handler cible
        # résout ses propres credentials dessus). Soft — sans jeton (dev local) il n'y
        # a pas de sub et tout le catalogue est déjà accessible. Un échec d'identité,
        # lui, n'est pas une absence de jeton : il monte (#464).
        sub = current_user_sub_from_token()

        # Le nom vient du catalogue, donc éventuellement sous la forme du tenant. Il
        # redevient canonique AVANT le gate méta/spine : sans ça `acme_doc` résout un
        # namespace inconnu, échappe à `_NON_DISPATCHABLE`, et l'anti-boucle saute.
        demande, name = name, tool_alias.canonical(name, _tool_prefix())
        # Un nom RETIRÉ refuse AVANT le gate méta/spine : `oto_kb` est un nom `oto_*`, et
        # « appelle-le directement » renverrait l'agent vers un nom qui n'existe plus.
        # C'est CE chemin qu'un agent prend quand une procédure nomme un outil absent de
        # sa liste — la notice le lui prescrit.
        _refuser_si_retire(name)
        if namespace_of(name) in _NON_DISPATCHABLE:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`{demande}` est un outil méta/spine — appelle-le directement, "
                        "pas via oto_call."))

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
        # `_run_id=` passé AU NIVEAU D'oto_call plutôt que dans `arguments` : replié
        # comme `_org`, pour la même raison. Le modèle voit `_org` en tête de schéma
        # et range le jeton frère au même endroit ; sans ce repli, l'appel échouait
        # (« Unexpected keyword argument ») alors que la notice lui demande de porter
        # `_run_id` sur CHAQUE appel. `setdefault` : ce qui est déjà dans `arguments`
        # gagne — c'est la forme documentée, elle ne doit pas se faire écraser.
        if _run_id is not None:
            args.setdefault("_run_id", _run_id)
        tool = await _resolve_tool(ctx, name)
        if tool is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{demande}`. Use oto_list_my_tools to see available names."))
        issue = await executer_cible(tool, sub, name, demande, args)
        if not issue.ok:
            # `tool` reprend le nom DEMANDÉ : l'agent le relit pour réessayer, et un
            # nom qu'il n'a jamais tapé le ferait douter de sa propre requête.
            return {"tool": demande, "ok": False, "error": issue.message}
        return issue.result

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
