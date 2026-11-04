"""`CallContextMiddleware` — les axes-contexte de l'appel (`_org=`, `_account=`, …)."""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp.server.middleware import Middleware
from mcp.types import ErrorData, INVALID_PARAMS
from starlette.concurrency import run_in_threadpool

from .. import (access, call_axes, guide_run, outils_du_travail, outils_retires, providers,
               redaction, run_org, session_org, verrou_org)
from ..mcp_errors import McpError
from ..access import credits as budget
from ..auth.hooks import current_user_sub_from_token
from ..connectors import access_level_gate, activation_gate
from ..tool_visibility import namespace_of

logger = logging.getLogger(__name__)


def _cible(name: str, args: dict) -> str:
    """L'outil réellement VISÉ par l'appel. `oto_call` en dispatche un autre (ADR
    0036) : c'est son `name` qui dit de quel connecteur il s'agit, pas le sien.

    Sans ça, l'écho de compte se taisait précisément là où il sert le plus. Le
    schéma MCP est figé au handshake : une session ouverte AVANT la pose d'un
    second compte ne voit pas l'axe `_account=` sur les outils du connecteur, donc
    l'agent n'a d'autre voie que `oto_call` pour viser un workspace — et c'est
    exactement l'appel qui ne lui disait pas sous quelle identité il était parti.
    Vécu en vrai le 27/08, sur le premier double workspace Slack."""
    if name != "oto_call":
        return name
    vise = args.get("name")
    return vise if isinstance(vise, str) and vise else name


def _echo_account(result, tool_name: str, rappel: Optional[dict] = None):
    """Dit à l'agent SOUS QUEL COMPTE l'appel est parti, quand il en a plusieurs — et,
    pour le compte d'une AUTRE personne, à qui il est : `rappel` = le descripteur
    (`access.rappel_du_compte` : `_account`, `lent_by`), par défaut `{_account}` seul.

    Un agent qui détient deux workspaces Slack en visait un — par défaut posé, par
    épinglage de projet ou par `_account=` — sans que rien ne le lui confirme :
    l'identité effective ne vivait que dans le journal, qu'il ne lit pas. Un envoi
    sous la mauvaise identité ne se rattrape pas ; le minimum est de la nommer. Même
    leçon pour un prêt : une emprunteuse a opéré la boîte d'une collègue sans le savoir,
    puis la sienne dès qu'elle l'a connectée, dans la même conversation, sans un signal.

    Trois gardes, dans cet ordre :
    - **compte NOMMÉ seulement** : en mono-compte la ligne du coffre est anonyme
      (`account=''`) ⟹ aucun écho, aucun bruit ajouté à 99 % des réponses ;
    - **même PORTEUR que l'outil appelé** : un outil composite peut résoudre un
      credential auxiliaire, et annoncer CE compte-là serait un mensonge. La comparaison
      se fait sous le porteur (`providers.credential_provider`) : un compte propre se
      résout sous la carte du service (`outlook`), un prêt sous la ligne du porteur
      (`microsoft`) — comparer les noms nus taisait l'écho précisément pour le prêt ;
    - **payload dict** : une liste ou du texte est rendu tel quel.

    Posé ici plutôt que dans un middleware de plus : c'est le pendant naturel de ce
    que ce middleware fait à l'aller (poser le contexte de l'appel), et il est plus
    EXTERNE que la rédaction — donc l'écho n'est ni redacté, ni observé comme un
    champ du connecteur par la capture de schéma. Best-effort : un écho ne fait
    jamais échouer un appel qui a réussi.
    """
    try:
        if getattr(result, "is_error", False):
            return result
        trace = session_org.current_call_trace() or {}
        account = trace.get("resolved_account") or ""
        # A mono-account row is anonymous (no echo) — unless it is an account of ANOTHER
        # org, which is always said (#1499, D1/#172).
        if not (account or trace.get("compte_d_une_autre_org")) or (
                providers.credential_provider(trace.get("resolved_connector") or "")
                != providers.credential_provider(namespace_of(tool_name))):
            return result
        payload = redaction.extract_payload(result)
        if not isinstance(payload, dict) or "_account" in payload:
            return result
        echo = rappel or {"_account": account}
        return redaction.rebuild_result(result, {**payload, **echo})
    except Exception:  # noqa: BLE001 — un écho ne casse pas un appel réussi
        logger.debug("écho du compte impossible", exc_info=True)
        return result


def _rappel_hors_boucle(account: str, row, autre_org: Optional[dict] = None) -> dict:
    """The descriptor of a MEMBER row that served: the owner's label reads the base —
    off-loop. A failed lookup still says the account is ANOTHER person's (by her sub):
    the signal matters more than the label."""
    sub = current_user_sub_from_token()
    try:
        return access.rappel_du_compte(sub, account, row, autre_org)
    except Exception:  # noqa: BLE001 — le prêt reste signalé, par son sub
        logger.warning("loan descriptor unreadable", exc_info=True)
        preteur = access.preteur_de_la_ligne(sub, row)
        return {"_account": account, **({"lent_by": preteur} if preteur else {})}


async def _rappel_de_l_appel() -> Optional[dict]:
    """The echo's descriptor. Nothing at all (no hop, no query) unless a MEMBER row
    served — the only kind a person lends. Never raises: it runs after a call that
    succeeded."""
    trace = session_org.current_call_trace() or {}
    account = trace.get("resolved_account") or ""
    # An account of ANOTHER org served (#1499, D1/#172): always said, named or not.
    autre_org = trace.get("compte_d_une_autre_org")
    if not account and not autre_org:
        return None
    row = trace.get("credential_row")
    if not access.ligne_d_un_membre(row):
        out = {"_account": account}
        if autre_org:   # pure: what the resolution noted, no read
            out.update({"your_account_in_org": autre_org["org"]} if autre_org.get("own")
                       else {"lent_by_org": autre_org["org"], "level": autre_org["level"]})
        return out
    try:
        return await run_in_threadpool(_rappel_hors_boucle, account, row, autre_org)
    except Exception:  # noqa: BLE001 — un écho ne casse pas un appel réussi
        logger.warning("loan descriptor unreadable", exc_info=True)
        return {"_account": account}


class CallContextMiddleware(Middleware):
    """Pose le contexte d'appel (`_org=`) AVANT toute la chaîne middleware, pour que la
    résolution du handler ET les hooks post-tool (rédaction de champs, calllog) voient
    la MÊME org que l'appel — pas l'org maison (modèle sans état de session, #108/#112).

    Doit être enregistré **en premier** (`add_middleware` : premier ajouté = plus
    EXTERNE, vérifié empiriquement sur fastmcp `_run_middleware`) → il enveloppe
    `FieldRedactionMiddleware` + `ToolCallLogger`, et la ContextVar `_CALL_ORG` reste
    posée pendant qu'ils relisent `current_org` (sinon reset trop tôt = rédaction/audit
    sous la maison — bug vécu jusqu'au 2026-08-02, le middleware était ajouté en
    dernier donc INNERMOST). ContextVar per-tâche (isolée par appel) ; reset en `finally`.

    Garde d'appartenance au point d'entrée : `_org=` dont le sub n'est pas membre lève un
    McpError **actionnable**, jamais un repli silencieux vers une autre org. Ne s'active
    que pour les tools de capacité, où `_org` est injecté au schéma par l'adaptateur
    (le préfixe `_` écarte toute collision avec un champ métier `org`, issue #250).
    """

    def __init__(self, reserved_org_tools):
        self._org = frozenset(reserved_org_tools)

    async def on_list_tools(self, context, call_next):
        """Advertise les axes-contexte plats (`_account=`, …) dans le schéma des tools
        CONCERNÉS (sélectif, `call_axes.axes_for`) → claude.ai sait les envoyer. Sans
        ça, `additionalProperties:false` ferait rejeter l'axe côté client. Les tools
        de capacité (`_org=`) sont schématisés par `_mcp_adapter`, pas ici."""
        tools = await call_next(context)
        # Axe compte DYNAMIQUE : annoncé sur les connecteurs où l'appelant détient
        # plusieurs clés (une requête, threadpool — chemin inbound mono-loop). Sans sub
        # (endpoint anonyme) → rien de plus que les axes statiques.
        # Un échec d'identité n'est pas une absence de jeton : il monte (#464).
        sub = current_user_sub_from_token()
        advertised = await run_in_threadpool(call_axes.account_axis_advertised_for, sub)
        # Access level (`connectors/access_level_gate.py`): under a RESTRICTING level, the
        # ops it does not cover leave the `op` enum of a multiplexed tool. `full` = {}.
        ops_couvertes = await run_in_threadpool(
            access_level_gate.restricted_ops, sub, {t.name for t in tools})
        out = []
        for t in tools:
            axes = call_axes.axes_for_listing(t.name, advertised)
            if axes:
                t = t.model_copy(update={
                    "parameters": call_axes.inject_schema(t.parameters, axes)})
            if t.name in ops_couvertes:
                t = t.model_copy(update={"parameters": access_level_gate.restrict_schema(
                    t.parameters, ops_couvertes[t.name])})
            out.append(t)
        return out

    async def on_call_tool(self, context, call_next):
        name = getattr(context.message, "name", "") or ""
        args = getattr(context.message, "arguments", None) or {}
        # A RETIRED name is refused FIRST, naming the call that works. The gates below
        # (a hosted job's tool list, connector activation) run before the error
        # envelope that classifies an unknown name: a hosted agent whose list was
        # migrated called `linkedin_unipile_chat` and read "not in this hosted agent's
        # tool list — add it", an instruction that cannot be followed.
        retire = outils_retires.retrait(name)
        if retire is not None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message=retire.message,
                data={"oto": {"code": "unknown_tool", "retryable": False,
                              "hint": retire.hint}}))
        # Pose chaque axe-contexte fourni pour CE tool, en collectant sa fonction de
        # reset AU MOMENT de la pose → reset LIFO dans le `finally` même si une pose
        # ultérieure lève (les tokens déjà posés sont toujours nettoyés).
        undo: list = []
        try:
            # LA FACE, avant tout le reste (oto#83) : cet appel est entré par un tool
            # MCP, donc il est piloté par un modèle. C'est la seule information sur la
            # nature de l'appelant que le serveur tient de lui-même — tout le reste
            # (`_run_id`, `client_id`, user-agent) est DÉCLARÉ par celui qu'on juge.
            # Elle sert au datastore à ne pas servir à un agent les colonnes que le
            # propriétaire d'un tableau garde pour lui (`agent_access`).
            #
            # Ici plutôt que dans les deux adaptateurs : ce middleware voit TOUS les
            # appels d'outils — capacités montées ET tools écrits à la main (`data_*`)
            # — et il est monté UNE fois par `_build_mcp`, sur le registre que servent
            # les deux faces, l'anonyme comprise. Deux poses vaudraient deux occasions
            # d'en oublier une.
            undo.append((session_org.reset_call_face,
                         session_org.set_call_face(session_org.FACE_MCP)))
            # L'outil APPELÉ, pour le verrou des rôles plateforme : un agent hébergé ne
            # garde le rôle de son porteur que pendant l'un des `oto_admin_*` que son
            # agent nomme (`verrou_org.outils_permis`). `oto_call` repose sa cible.
            undo.append((verrou_org.lever_outil, verrou_org.poser_outil(name)))
            # Relevé de résolution : posé EN PREMIER (donc reset EN DERNIER, LIFO) pour
            # que les seams l'aient pendant tout le handler ET que le calllog le relise
            # après. Inerte si rien ne le remplit — un dict vide n'ajoute aucune ligne.
            undo.append((session_org.reset_call_trace, session_org.set_call_trace({})))
            # Le RUN ACTIF de la session, posé en ContextVar pour les seams SYNC
            # (#317). Sans lui, un agent qui encadre son travail par `run_start` n'est
            # reconnu nulle part : la pile vit dans l'état de session (async), que le
            # store ne peut pas lire. Vécu en production le 15/08 — les lignes
            # n'étaient jamais rattachées à leur run, donc jamais libérées à sa
            # fermeture, et leur propre titulaire se voyait refuser l'écriture.
            #
            # ⚠️ MÊME source que le calllog (`server.py`) : le jeton explicite `_run_id=`
            # d'abord, la pile ensuite. J'avais pris le premier pour le run courant —
            # or il n'est posé que si l'appelant l'a passé, ce qu'un agent ne fait pas.
            # Une seule lecture des deux sources, ici, plutôt qu'une par seam.
            #
            # Une session NEUVE d'un agent hébergé (relais relancé après un appel coupé)
            # reprend d'abord le run que son jeton de délégation tient ouvert : poussé
            # sur la pile de CETTE session, que lisent le journal (`server.
            # _calllog_sink`) et la cible d'`oto_call` (`tools/meta`) — sans lui, la
            # timeline du run s'arrêtait à la session perdue. L'état de session se lit
            # sur le contexte FastMCP, pas sur celui du middleware.
            #
            # ⚠️ Le run de la pile se pose en RUN DE SESSION, pas en run de l'appel
            # (#830) : `current_call_run()` reste le seul `_run_id` explicite, que lisent
            # la résolution d'org du run (`run_org.pin_for_call`, #639 — refus si l'on
            # n'est plus membre) et les gardes anti-agent d'`oto_fleet`. Le run de
            # session ne sert qu'aux lecteurs qui RATTACHENT (datastore, gestes, jobs),
            # par `session_org.current_call_or_session_run()`. La pile se lit sur
            # `fastmcp_context` : le `MiddlewareContext` n'a pas `get_state`, et ce
            # repli lu sur lui est resté mort du 28/08 au 27/10/2026.
            fctx = getattr(context, "fastmcp_context", None)
            await guide_run.reprendre_run_du_jeton(fctx)
            # Seulement si l'appel ne NOMME aucun run : `_run_id` n'est posé que plus
            # bas (axes), et un run nommé ne se double pas de celui de la session.
            if (fctx is not None and not session_org.current_call_run()
                    and not args.get(call_axes.RUN.param)):
                actif = await guide_run.active_run_id(fctx)
                if actif:
                    undo.append((session_org.reset_session_run,
                                 session_org.set_session_run(actif)))
            # `_org=` (tools de capacité) : posé ici, retiré des kwargs par `_make_tool`.
            if name in self._org and args.get("_org") is not None:
                undo.append((session_org.reset_call_org, await self._pin_org(args["_org"])))
            # Axes plats (`_account=`, … — connecteurs/data) : lus des args BRUTS, posés,
            # puis RETIRÉS des arguments avant le dispatch (la fonction du tool ne les
            # déclare pas → elle validerait en erreur sinon). Les seams de résolution
            # existants (resolve_credential…) lisent la ContextVar.
            for axis in call_axes.axes_for_call(name):
                if axis.param in args:
                    undo.extend(await axis.pin_for(args.pop(axis.param), name))
            # `_run_id=` est ACCEPTÉ PARTOUT, jamais un motif de refus.
            #
            # La notice servie au handshake dit « `_run_id` sur CHAQUE appel dès qu'un
            # run est ouvert » ; l'axe, lui, n'est advertisé/lu que sur la surface de
            # TRAVAIL (coût jetons de `tools/list`). Les 53 capacités `oto_*` restantes
            # le voyaient donc arriver sans le déclarer, et le schéma plat le refusait
            # AVANT le handler (« Unexpected keyword argument ») : deux consignes du
            # même serveur qui se contredisent, et un agent qui obéit perd son appel.
            # Vécu trois fois — #168 (spine projet), puis les signaux #651
            # (`oto_trigger`) et #664 (`oto_procedure`), tous deux le 02/09/2026. Les
            # deux premières fois on a nommé le tool ; ici on ferme la CLASSE, comme
            # `oto_call` le fait déjà pour sa cible (`strip_unconsumed_axes`).
            #
            # ⚠️ RETIRÉ, pas posé. Poser la ContextVar ferait résoudre l'org du run
            # (`run_org.pin_for_call`) sur des outils qui ne l'ont jamais fait — donc
            # de NOUVEAUX refus (« tu n'es pas membre de l'org de ce run ») sur des
            # appels qui passaient, et réveillerait les gardes anti-agent d'`oto_fleet`
            # sur un chemin qui ne les a jamais vues. Élargir la CORRÉLATION est un
            # autre lot, avec sa propre mesure ; ici on ne fait que cesser de refuser.
            if name not in call_axes.RUN_SELF_HANDLED:
                args.pop(call_axes.RUN.param, None)
            # L'org du RUN (#639) : APRÈS les axes — un `_org=`/`_project=` explicite a
            # déjà posé l'org et garde la priorité ; sinon un appel qui porte un run se
            # résout dans l'org du run, appartenance gardée (refus nommé, jamais un
            # repli sur la maison). Une lecture par run, hors boucle.
            undo.extend(await run_org.pin_for_call())
            # La liste d'outils d'un travail hébergé, tenue par le serveur et pas
            # seulement par le worker — même garde, même place, que dans `oto_call`.
            await outils_du_travail.exiger(name)
            # L'activation du connecteur, contre l'org et l'équipe que le contexte vient
            # de poser — même garde, même place, que dans `oto_call` (#1064).
            await activation_gate.require_active(name)
            # The access level of the connector, same context, fail-closed — it reads the
            # `op` of the arguments (same guard, same place, as in `oto_call`).
            await access_level_gate.require_covered(
                name, args, lambda: self._schema_of(context, name))
            # Le budget d'unités : un outil du périmètre reçoit son relevé (identifiant,
            # arguments) avant de partir, la résolution de la clé plateforme y réserve.
            budget.ouvrir_appel(name, args)
            # What the call does (its op's class, `tools/effets`): the level the account
            # resolved for it must give (`access/niveaux`, #1499) — judged at resolution,
            # on the account REALLY resolved. Same note, same place, as in `oto_call`.
            undo.append((access.oublier_l_appel, access.noter_l_appel(
                name, args, await self._en_lecture(context, name))))
            try:
                result = await call_next(context)
            except Exception as e:
                # L'échec se règle aussi (budget seulement) : un refus net libère, le
                # reste se règle au montant réservé — puis l'erreur de l'appel repart.
                await run_in_threadpool(access.regler_echec, current_user_sub_from_token(),
                                        name, session_org.current_call_trace(), e)
                raise
            # Le règlement de l'appel (L8), tant que son contexte (org, équipe) est
            # encore posé — hors boucle : il écrit les compteurs.
            await run_in_threadpool(access.regler_appel, current_user_sub_from_token(),
                                    name, session_org.current_call_trace())
            return _echo_account(result, _cible(name, args),
                                 rappel=await _rappel_de_l_appel())
        finally:
            for reset, tok in reversed(undo):
                reset(tok)

    @staticmethod
    async def _en_lecture(context, name: str) -> bool:
        """Does the tool declare itself read-only (`tools/lecture`)? Read on its FastMCP
        object; a relay (`oto_call`) is judged on its target, by `executer_cible`."""
        if name == "oto_call":
            return False
        serveur = getattr(getattr(context, "fastmcp_context", None), "fastmcp", None)
        if serveur is None:    # no server to declare it: judged on its op (stricter)
            return False
        from ..tools import lecture
        return lecture.en_lecture(await serveur.get_tool(name))

    @staticmethod
    async def _schema_of(context, name: str):
        outil = await context.fastmcp_context.fastmcp.get_tool(name)
        return getattr(outil, "parameters", None)

    @staticmethod
    async def _pin_org(org):
        # Garde partagée (`resolve_org_guarded`) = MÊME résolution qu'`oto_use_org` +
        # McpError propre (ce middleware est outermost → une exception opaque serait
        # invisible à Sentry, vécu prod 2026-07-04). Idem l'axe plat `_org=` et oto_call.
        return session_org.set_call_org(await call_axes.resolve_org_guarded(org))
