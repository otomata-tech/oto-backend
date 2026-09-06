"""`CallContextMiddleware` — les axes-contexte de l'appel (`_org=`, `_account=`, …)."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware
from starlette.concurrency import run_in_threadpool

from .. import call_axes, guide_run, redaction, run_org, session_org
from ..auth.hooks import current_user_sub_from_token
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


def _echo_account(result, tool_name: str):
    """Dit à l'agent SOUS QUEL COMPTE l'appel est parti, quand il en a plusieurs.

    Un agent qui détient deux workspaces Slack en visait un — par défaut posé, par
    épinglage de projet ou par `_account=` — sans que rien ne le lui confirme :
    l'identité effective ne vivait que dans le journal, qu'il ne lit pas. Un envoi
    sous la mauvaise identité ne se rattrape pas ; le minimum est de la nommer.

    Trois gardes, dans cet ordre :
    - **compte NOMMÉ seulement** : en mono-compte la ligne du coffre est anonyme
      (`account=''`) ⟹ aucun écho, aucun bruit ajouté à 99 % des réponses ;
    - **même connecteur que l'outil appelé** : un outil composite peut résoudre un
      credential auxiliaire, et annoncer CE compte-là serait un mensonge ;
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
        if not account or trace.get("resolved_connector") != namespace_of(tool_name):
            return result
        payload = redaction.extract_payload(result)
        if not isinstance(payload, dict) or "_account" in payload:
            return result
        return redaction.rebuild_result(result, {**payload, "_account": account})
    except Exception:  # noqa: BLE001 — un écho ne casse pas un appel réussi
        logger.debug("écho du compte impossible", exc_info=True)
        return result


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
        sub = None
        try:
            sub = current_user_sub_from_token()
        # noqa: SILENT — axe compte dynamique optionnel : sans lui, les axes statiques suffisent
        except Exception:
            pass
        advertised = await run_in_threadpool(call_axes.account_axis_advertised_for, sub)
        out = []
        for t in tools:
            axes = call_axes.axes_for_listing(t.name, advertised)
            if axes:
                t = t.model_copy(update={
                    "parameters": call_axes.inject_schema(t.parameters, axes)})
            out.append(t)
        return out

    async def on_call_tool(self, context, call_next):
        name = getattr(context.message, "name", "") or ""
        args = getattr(context.message, "arguments", None) or {}
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
            # — et il est monté sur chaque instance par `_build_mcp`, l'anonyme
            # comprise. Deux poses valent deux occasions d'en oublier une.
            undo.append((session_org.reset_call_face,
                         session_org.set_call_face(session_org.FACE_MCP)))
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
            if not session_org.current_call_run():
                actif = await guide_run.active_run_id(context)
                if actif:
                    undo.append((session_org.reset_call_run,
                                 session_org.set_call_run(actif)))
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
            return _echo_account(await call_next(context), _cible(name, args))
        finally:
            for reset, tok in reversed(undo):
                reset(tok)

    @staticmethod
    async def _pin_org(org):
        # Garde partagée (`resolve_org_guarded`) = MÊME résolution qu'`oto_use_org` +
        # McpError propre (ce middleware est outermost → une exception opaque serait
        # invisible à Sentry, vécu prod 2026-07-04). Idem l'axe plat `_org=` et oto_call.
        return session_org.set_call_org(await call_axes.resolve_org_guarded(org))
