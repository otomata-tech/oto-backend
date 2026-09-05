"""Capacité « flottes du runner » — la configuration déclarée d'un passage (R4).

Deux faces, et la frontière n'est pas celle de `runner.jobs`. La file de jobs est
worker-only : c'est de la plomberie d'exécution, elle n'a pas de face agent. Une
FLOTTE est de la config utilisateur — « fais tourner cette procédure sur ce
tableau, dans ce périmètre, jusqu'à telle borne » — au même titre qu'un
déclencheur. Elle se pose en conversation comme au dashboard, et surtout elle se
LIT : l'état d'un passage n'existait jusqu'ici que parce qu'une session poussait
des messages à une autre.

⚠️ **Un lancement vise une flotte déclarée, jamais un tableau passé en argument.**
Un verbe généraliste rendrait accessible en un appel le geste qu'on a passé deux
jours à empêcher — lancer des agents sur le fichier d'un client, sans cible
constatée, sans périmètre, sans borne. Et l'argument porte plus loin que le
risque : **une configuration déclarée est l'endroit où les gardes VIVENT.** Un
lancement libre n'a nulle part où accrocher une cible ni un plafond. Déclarer
n'est pas restreindre : c'est donner un domicile.

⚠️ **La CIBLE ne se modifie pas.** `namespace` et `row_filter` sont figés à la
déclaration : rediriger un passage en vol vers un autre tableau est précisément
ce contre quoi la déclaration existe. La frontière compte d'autant plus que les
bancs d'essai et la production d'un client ne vivent pas dans la même org.
Et il y a une raison de plus, qui vaut même sans malveillance : **une cible
mutable rend toute mesure INATTRIBUABLE.** Un relevé de coût ou d'avancement ne
veut plus rien dire si la cible a bougé entre le lancement et la lecture, et
personne ne peut savoir après coup laquelle il a mesurée. Le besoin légitime de
viser autre chose se règle comme partout ailleurs : **on duplique, on ne fait pas
basculer** — une nouvelle flotte, pas une flotte modifiée.

⚠️ **Lancer et arrêter sont SERVIS ici** — la question de qui a le droit a été
tranchée, et elle l'a été garde par garde plutôt qu'en fermant les verbes.

Ce paragraphe a affirmé le contraire jusqu'au 02/09 (« cette capacité déclare et
lit »), et l'argument tenait : les deux faces aboutissent au même handler, donc
servir `stop` le rend appelable par un AGENT — qui pourrait couper le passage
qui le fait tourner. **Fermer le verbe payait ce risque sur tous les usages
légitimes**, à commencer par le plus utile : un opérateur qui pilote sa campagne
par la conversation. La garde nomme donc le cas au lieu de fermer la porte —
`not_your_own_fleet` refuse d'arrêter la flotte qui exécute le déroulé courant,
et laisse passer tout le reste.

Ce qui reste vrai, et qui est la vraie asymétrie : **`launch` ARME**, il ne
démarre aucun processus, et il demande un ADMIN d'org ; **`stop` DEMANDE**
l'arrêt et l'ouvre à tout membre — attendre un admin pendant qu'une flotte
dépense est le mauvais échange.

Et les trois verbes de l'ORDONNANCEUR sont servis ici aussi (`take`, `beat`,
`ack_stop`). Ils ont manqué au contrat d'entrée du 30/08 au 02/09 pendant que la
base et le runner les portaient : `op=stop` écrivait alors un ordre que personne
ne pouvait lire, et une campagne annoncée « en arrêt » continuait de dépenser.
**Un ordre que personne ne peut lire est un ordre qui n'arrive jamais.**
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from . import _instruction
from .. import access, db
from ..tool_visibility import BETA_OPTION

logger = logging.getLogger(__name__)
from ._authz import ORG_MEMBER
from ._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                     RestBinding)
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)


def _run_courant() -> Optional[str]:
    """Le déroulé qui porte CET appel, ou None hors d'un run.

    ⚠️ `ResolvedCtx` ne porte PAS le run : le lire depuis `ctx` rendrait toujours
    `None` et les gardes anti-agent seraient DÉCORATIVES — vertes, et inertes.
    Il vit dans le contexte d'appel, posé par l'axe `_run_id`.
    """
    from .. import session_org
    return session_org.current_call_run()

# Ce qui pilote l'appel plutôt que la configuration : jamais « posé », donc jamais
# compté comme un champ inerte par les gardes de seam.
_STRUCTURELS = frozenset({"op", "fleet_id"})

# Les BORNES d'exploitation, et le plancher qu'elles partagent : une borne se
# compte, donc elle vaut au moins 1. ⚠️ `max_rows=-5`, `max_tokens=-1` ou
# `workers=0` passaient des DEUX côtés — une borne absurde acceptée est une panne
# différée, et elle se découvre au lancement plutôt qu'à la déclaration.
_BORNES = ("workers", "max_rows", "max_tokens", "max_consecutive_failures",
           "max_tokens_per_row", "max_steps")


def _bornes_valides(inp: "FleetInput") -> None:
    fautives = {c: v for c in _BORNES
                if (v := getattr(inp, c)) is not None and v < 1}
    if fautives:
        raise AuthzDenied(
            400, "invalid_bound",
            "une borne se compte, donc elle vaut au moins 1 : "
            + ", ".join(f"`{c}`={v}" for c, v in sorted(fautives.items())))

class FleetInput(BaseModel):
    op: Literal["create", "list", "get", "state", "update", "launch", "stop",
                "take", "beat", "ack_stop"]
    fleet_id: Optional[int] = None
    status: Optional[str] = None
    # create —
    label: Optional[str] = None
    procedure: Optional[str] = None
    tools: Optional[list[str]] = None
    namespace: Optional[str] = None
    row_filter: Optional[dict] = None
    project_id: Optional[int] = None
    input: Optional[str] = None
    max_steps: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    workers: Optional[int] = None
    max_rows: Optional[int] = None
    max_tokens: Optional[int] = None
    max_consecutive_failures: Optional[int] = None
    max_tokens_per_row: Optional[int] = None
    # stop — la raison est ÉCRITE : « arrêtée » sans raison oblige à rouvrir les
    # journaux pour savoir si c'était un incident, un budget ou une décision.
    reason: Optional[str] = None


class Fleet(BaseModel):
    """Une flotte telle que servie (les colonnes de `_COLS`, db/runner_fleets)."""
    id: int
    org_id: Optional[int] = None
    sub: Optional[str] = None
    label: Optional[str] = None
    procedure: Optional[str] = None
    project_id: Optional[int] = None
    tools: Optional[list[str]] = None
    input: Optional[str] = None
    max_steps: Optional[int] = None
    namespace: Optional[str] = None
    row_filter: Optional[dict] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    workers: Optional[int] = None
    rows_at_launch: Optional[int] = Field(
        None,
        description=(
            "Combien de lignes visaient le passage au moment de l'armement — le "
            "DÉNOMINATEUR de son avancement, relu à chaque armement. Ce n'est pas "
            "une borne (`max_rows` est le plafond déclaré) : c'est ce que la table "
            "contenait vraiment. `null` = pas de cible, ou compte illisible — "
            "« inconnu », jamais zéro."),
    )
    max_rows: Optional[int] = None
    max_tokens: Optional[int] = None
    max_consecutive_failures: Optional[int] = None
    max_tokens_per_row: Optional[int] = None
    status: Optional[str] = None
    stop_reason: Optional[str] = None
    armed_at: Optional[str] = None
    started_at: Optional[str] = None
    stopping_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    stopped_at: Optional[str] = None
    created_at: Optional[str] = None


class FleetState(BaseModel):
    """L'avancement d'un passage, agrégé sur ses travaux.

    `no_jobs_attached` est DÉCLARÉ plutôt que déduit de compteurs à zéro :
    un zéro qui peut vouloir dire « rien trouvé » ou « personne n'a regardé » est
    le défaut qui a coûté le plus cher sur ce chantier.
    """
    jobs_total: int
    pending: Optional[int] = None
    claimed: Optional[int] = None
    done: Optional[int] = None
    failed: Optional[int] = None
    abandoned: Optional[int] = None
    usage_tokens: Optional[int] = None
    heaviest_row_tokens: Optional[int] = None
    last_finished: Optional[str] = None
    no_jobs_attached: bool


class FleetOut(BaseModel):
    fleet: Optional[Fleet] = None
    # `beat` : l'ordre lu dans le même appel que le battement — un ordonnanceur
    # qui bat sans demander « dois-je m'arrêter ? » laisserait `stopping` sans
    # lecteur, et l'arrêt resterait une intention.
    stop_requested: Optional[bool] = None
    beat_taken: Optional[bool] = None
    fleets: Optional[list[Fleet]] = None
    state: Optional[FleetState] = None


def _lignes_visees(ctx: ResolvedCtx, fleet_id: int) -> Optional[int]:
    """Combien de lignes le passage vise MAINTENANT — lu sur la table, avec le
    `row_filter` figé à la déclaration, au moment de l'armer.

    C'est le seul instant où ce compte est vrai : les agents le font baisser dès
    la première ligne traitée, donc personne ne peut le reconstituer après coup.

    ⚠️ Fail-OPEN, et tracé. Une table supprimée ou un filtre devenu invalide ne
    doit pas empêcher d'armer : le passage part avec un dénominateur inconnu, ce
    que l'écran sait dire. Refuser ici transformerait un défaut d'affichage en
    panne de lancement.

    ⚠️ Compté sous l'identité de QUI ARME, pas de l'agent qui travaillera — ce
    sont deux regards différents sur la même table (filtres de champs, partages).
    Le compte dit donc « ce que voyait celui qui a lancé », et c'est suffisant
    pour un dénominateur d'écran ; ça ne le serait pas pour une borne, et c'est
    une des raisons pour lesquelles il n'en est pas une (cf. le banc qui sépare
    `rows_at_launch` de `max_rows`).
    """
    f = db.get_fleet(fleet_id, ctx.org_id)
    if not f or not f.get("namespace"):
        return None
    try:
        from ..datastore.core import make_store
        return make_store(ctx.sub).count_rows(f["namespace"],
                                              filter=f.get("row_filter") or None)
    except Exception:
        logger.warning("compte de lignes illisible pour la flotte %s (table %s)",
                       fleet_id, f.get("namespace"), exc_info=True)
        return None


def _fleets(ctx: ResolvedCtx, inp: FleetInput) -> dict:
    if not ctx.org_id:
        raise AuthzDenied(400, "org_required", "les flottes sont org-scopées")
    # ⚠️ Bêta = une GARDE, pas une visibilité. `session_visibility` masque
    # `oto_fleet` de la LISTE d'outils des comptes sans l'option ; mais la même
    # capacité est servie en REST (`/api/me/runner/fleets`) et joignable par
    # `oto_call` — deux chemins qui ne lisent aucune liste. Sans ce refus, un
    # membre non bêta déclarait, lançait et arrêtait des passages depuis un front,
    # alors que son agent ne voyait même pas le nom. Fail-CLOSED, comme la
    # visibilité : une bêta qui s'ouvre sur un hoquet ne se voit pas.
    try:
        beta = access.has_option(ctx.sub, BETA_OPTION, org=ctx.org_id)
    except Exception:
        # Fermer sans le dire serait un silence ; on ferme ET on le trace.
        logger.warning("beta gate fail-CLOSED for %s in org %s",
                       ctx.sub, ctx.org_id, exc_info=True)
        beta = False
    if not beta:
        raise AuthzDenied(
            403, "beta_required",
            "les passages d'agents sont en bêta : un admin pose l'option `beta` sur "
            "ton compte ou ton org (`oto_admin_set_option`)")

    if inp.op == "create":
        # ⚠️ Le seam vaut pour TOUTE opération, pas pour le seul verbe qu'on avait
        # regardé. `create status="running"` rendait 200 avec une flotte `draft` et
        # le champ avalé — mot pour mot le geste que le refus d'`update` prédit.
        # Une garde écrite dans une branche ne garde que cette branche.
        if inp.status is not None:
            raise AuthzDenied(
                400, "status_not_settable",
                "l'état d'un passage ne se pose pas à la création — une flotte naît "
                "`draft`. `status` ne sert qu'à FILTRER `list`.")
        if inp.fleet_id is not None:
            raise AuthzDenied(
                400, "field_not_settable",
                "`create` ne prend pas `fleet_id` : l'identifiant est attribué par "
                "la plateforme.")
        _bornes_valides(inp)
        manquants = [c for c in ("label", "procedure", "tools") if not getattr(inp, c)]
        if manquants:
            raise AuthzDenied(
                400, "missing_fields",
                f"create exige : {', '.join(manquants)} — le nom du passage, la "
                "procédure à jouer, et les outils (l'allowlist du run)")
        # La cible se DÉCLARE ou s'assume absente : un passage qui écrit dans un
        # tableau sans l'avoir nommé n'a aucun périmètre à opposer à un agent.
        if inp.row_filter is not None and not inp.namespace:
            raise AuthzDenied(
                400, "target_incomplete",
                "`row_filter` sans `namespace` : un périmètre suppose un tableau. "
                "Nomme la cible, ou n'en déclare aucune.")
        return {"fleet": db.create_fleet(
            ctx.org_id, ctx.sub, label=inp.label, procedure=inp.procedure,
            tools=inp.tools, namespace=inp.namespace, row_filter=inp.row_filter,
            project_id=inp.project_id, max_steps=inp.max_steps,
            input=inp.input or _instruction.de_file(
                inp.procedure, inp.namespace, inp.row_filter),
            provider=inp.provider, model=inp.model, workers=inp.workers or 1,
            max_rows=inp.max_rows, max_tokens=inp.max_tokens,
            max_consecutive_failures=inp.max_consecutive_failures,
            max_tokens_per_row=inp.max_tokens_per_row)}

    if inp.op == "list":
        return {"fleets": db.list_fleets(ctx.org_id, inp.status)}

    if inp.fleet_id is None:
        raise AuthzDenied(400, "missing_fields", f"{inp.op} exige `fleet_id`")

    if inp.op == "get":
        f = db.get_fleet(inp.fleet_id, ctx.org_id)
        if not f:
            raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
        return {"fleet": f}

    if inp.op == "launch":
        # ⚠️ PLANCHER ADMIN — et il ne suit pas le geste, il suit ce que le geste
        # ENGAGE. Lancer emporte des effets externes irréversibles : de l'argent
        # dépensé, des lignes écrites chez un tiers. Arrêter coûte une reprise.
        # Le dépôt tient déjà ce motif ailleurs (écrire ouvert, supprimer au chef)
        # : la garde suit le VERBE, pas l'objet.
        from .. import roles
        if not roles.is_org_admin(ctx.sub, ctx.org_id):
            raise AuthzDenied(
                403, "org_admin_required",
                "lancer un passage est réservé aux administrateurs de l'org : il "
                "engage une dépense et des écritures chez un tiers. L'ARRÊTER, en "
                "revanche, est ouvert à tout membre.")
        # ⚠️ Un déroulé ne LANCE pas. Un agent qui se relance lui-même coûte un
        # budget en boucle — pire qu'un arrêt de trop, qui ne coûte qu'une reprise.
        if _run_courant():
            raise AuthzDenied(
                403, "not_from_a_run",
                "un déroulé ne lance pas de passage — un agent qui se relance "
                "lui-même dépense en boucle.")
        # ⚠️ Une campagne déclarée avant que la plateforme compose — ou par une
        # surface qui a laissé le champ vide — n'a pas d'instruction. L'armer
        # telle quelle la rend MUETTE au premier passage : le worker refuse de
        # démarrer sans instruction, la flotte reste `armed`, et le symptôme lu
        # depuis le produit est « l'ordonnanceur est mort » — un diagnostic faux
        # posé sur une cause invisible. On répare AVANT d'armer, jamais après.
        avant = db.get_fleet(inp.fleet_id, ctx.org_id)
        if avant and avant.get("procedure") and not (avant.get("input") or "").strip():
            db.update_fleet(inp.fleet_id, ctx.org_id, {"input": _instruction.de_file(
                avant["procedure"], avant.get("namespace"), avant.get("row_filter"))})
        # ⚠️ UN SEUL armement, qui porte le dénominateur (#836). La fusion des deux
        # lots en avait produit deux : #873 arme après avoir réparé, #836 arme avec
        # `rows_at_launch` — les garder tous les deux armait avant ET après la
        # réparation, ce qui défait précisément l'ordre que #873 existe pour tenir.
        f = db.armer(inp.fleet_id, ctx.org_id,
                     rows_at_launch=_lignes_visees(ctx, inp.fleet_id))
        if not f:
            actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
            if not actuelle:
                raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
            raise AuthzDenied(
                409, "not_launchable",
                f"ce passage est `{actuelle['status']}` — on n'arme que ce qui ne "
                "tourne pas. Arrête-le d'abord, ou déclare une autre flotte.")
        return {"fleet": f}

    if inp.op == "stop":
        # Ouvert à TOUT MEMBRE : un passage qui part en vrille doit pouvoir être
        # stoppé par la première personne qui le voit, pas par celle qui a le bon
        # rôle. Attendre un admin pendant qu'une flotte dépense est le mauvais
        # échange.
        # ⚠️ Mais un déroulé n'arrête pas CELLE QUI L'EXÉCUTE : la garde nomme le
        # cas plutôt que de fermer le verbe à tout le monde — fermer, ce serait
        # payer le prix sur tous les usages légitimes.
        run = _run_courant()
        if run and db.run_appartient_a_flotte(run, inp.fleet_id):
            raise AuthzDenied(
                403, "not_your_own_fleet",
                "un déroulé ne peut pas arrêter le passage qui l'exécute.")
        f = db.demander_arret(inp.fleet_id, ctx.org_id,
                              inp.reason or "arrêt demandé")
        if not f:
            actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
            if not actuelle:
                raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
            raise AuthzDenied(
                409, "not_stoppable",
                f"ce passage est `{actuelle['status']}` — il n'y a rien à arrêter.")
        # ⚠️ `stopping`, pas `stopped` : l'ordre est POSÉ, la boucle ne l'a pas
        # encore lu. Le passage continue jusqu'à ce qu'elle accuse réception.
        return {"fleet": f}

    # ── Les gestes de l'ORDONNANCEUR — ceux qui transforment une intention en
    # fait. Ils sont servis parce que sans eux `op=stop` reste une écriture que
    # personne ne lit ; et ils POSENT les faits que les verbes d'opérateur
    # n'ont pas le droit de poser.
    if inp.op == "take":
        f = db.prendre(inp.fleet_id, ctx.org_id)
        if not f:
            actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
            if not actuelle:
                raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
            # ⚠️ Refus et non 200 : deux ordonnanceurs qui prendraient la même
            # flotte armée doubleraient le passage. Le premier gagne, le second
            # l'apprend au lieu de partir en croyant l'avoir prise.
            raise AuthzDenied(
                409, "not_takeable",
                f"ce passage est `{actuelle['status']}` — on ne prend qu'une "
                "flotte `armed`. Un autre ordonnanceur l'a peut-être déjà prise.")
        return {"fleet": f}

    if inp.op == "beat":
        # Le battement, ET la lecture de l'ordre dans le même appel : un
        # ordonnanceur qui bat sans jamais demander « dois-je m'arrêter ? »
        # laisserait `stopping` sans lecteur.
        vivant = db.battre(inp.fleet_id, ctx.org_id)
        f = db.get_fleet(inp.fleet_id, ctx.org_id)
        if not f:
            raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
        return {"fleet": f, "stop_requested": f["status"] in ("stopping", "stopped"),
                "beat_taken": vivant}

    if inp.op == "ack_stop":
        # ⚠️ Le SEUL geste qui pose `stopped`, et c'est l'ordonnanceur qui le
        # pose. Si un opérateur pouvait l'écrire, l'écart entre « demandé » et
        # « effectif » disparaîtrait — et avec lui le seul diagnostic d'un
        # ordonnanceur mort.
        if not db.accuser_arret(inp.fleet_id, ctx.org_id, inp.reason):
            actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
            if not actuelle:
                raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
            raise AuthzDenied(
                409, "nothing_to_acknowledge",
                f"ce passage est `{actuelle['status']}` — il n'y a pas d'arrêt en "
                "cours à accuser.")
        return {"fleet": db.get_fleet(inp.fleet_id, ctx.org_id)}

    if inp.op == "state":
        etat = db.fleet_state(inp.fleet_id, ctx.org_id)
        if not etat:
            raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
        return etat

    # update — partiel, et jamais sur la cible ni sur l'état.
    champs: dict[str, Any] = {c: getattr(inp, c) for c in db.CHAMPS_MODIFIABLES
                              if getattr(inp, c) is not None}
    if inp.namespace is not None or inp.row_filter is not None:
        raise AuthzDenied(
            400, "target_is_frozen",
            "la cible d'un passage ne se modifie pas — `namespace` et `row_filter` "
            "sont figés à la déclaration. Un autre tableau, c'est une autre flotte.")
    if inp.provider is not None or inp.model is not None:
        raise AuthzDenied(
            400, "context_is_frozen",
            "le contexte d'exécution ne se modifie pas — `provider` et `model` sont "
            "figés à la déclaration. Les changer en vol rendrait FAUSSE l'attribution "
            "des lignes déjà écrites sous ce passage. Déclare une autre flotte.")
    # ⚠️ `status` figure dans l'entrée parce qu'il FILTRE `list`. Le laisser tomber
    # en silence ici rendrait 200 avec la flotte inchangée — et c'est précisément le
    # geste qu'un agent privé de `stop` tenterait, en lisant un succès dans la
    # réponse. Un vide ne doit jamais se lire comme un fait.
    if inp.status is not None:
        raise AuthzDenied(
            400, "status_not_settable",
            "l'état d'un passage ne se pose pas par `update` — `status` ne sert ici "
            "qu'à FILTRER `list`. Démarrer et arrêter un passage appartiennent à "
            "l'ordonnanceur, et ne sont servis par aucune face de cette capacité.")
    # ⚠️ La garde appartient au SEAM, pas au champ. Écrite champ par champ, elle
    # oublie exactement ceux auxquels personne n'a pensé : `procedure` — ce que la
    # flotte EXÉCUTE — et `project_id` rendaient 200 sans le moindre effet. Tout
    # champ d'entrée qui n'est ni STRUCTUREL ni modifiable aboutit, ou se refuse ;
    # et le refus vaut aussi pour ceux qu'on ajoutera à l'entrée demain.
    _bornes_valides(inp)
    # ⚠️ Ce que `create` EXIGE, `update` ne doit pas pouvoir l'annuler : `tools=[]`
    # était refusé à la création et vidait l'allowlist par retouche. Une garde qui
    # ne tient qu'à l'entrée laisse la sortie ouverte.
    if inp.tools is not None and not inp.tools:
        raise AuthzDenied(
            400, "missing_fields",
            "`tools` ne peut pas être vidé — c'est l'allowlist du run, et `create` "
            "l'exige. Une flotte sans outils n'exécute rien.")
    fournis = {c for c, v in inp.model_dump(exclude_none=True).items()
               if c not in _STRUCTURELS}
    inertes = sorted(fournis - set(db.CHAMPS_MODIFIABLES))
    if inertes:
        raise AuthzDenied(
            400, "field_not_settable",
            f"`update` ne pose pas : {', '.join(inertes)}. Ces champs se déclarent "
            "à la création et ne se retouchent pas — une autre valeur, c'est une "
            "autre flotte. Les champs modifiables sont : "
            f"{', '.join(db.CHAMPS_MODIFIABLES)}.")
    f = db.update_fleet(inp.fleet_id, ctx.org_id, champs)
    if not f:
        raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
    return {"fleet": f}


CAPABILITIES += [
    Capability(
        key="runner.fleets",
        handler=_fleets,
        Input=FleetInput,
        Output=FleetOut,
        authz=ORG_MEMBER,
        mcp="oto_fleet",
        # Les refus PUBLIÉS. Un dashboard doit pouvoir GRISER un champ plutôt que
        # laisser tenter un geste qui sera refusé — c'est exactement ce qu'un front
        # tiers n'a pas pu faire le 29/08 sur une borne qui n'était écrite nulle
        # part de servi. ⚠️ Chacun est REJOUÉ sur la route servie
        # (`tests/api/test_runner_fleets_rest.py`) : une déclaration sans rejeu
        # promet un statut que le serveur ne rend peut-être pas.
        errors=(
            DeclaredError(400, "missing_fields",
                          "`create` sans `label`/`procedure`/`tools`, ou opération "
                          "sur une flotte sans `fleet_id`"),
            DeclaredError(400, "target_incomplete",
                          "`row_filter` sans `namespace` — un périmètre suppose un "
                          "tableau"),
            DeclaredError(400, "target_is_frozen",
                          "`namespace`/`row_filter` après la déclaration : la cible "
                          "d'un passage ne se déplace pas"),
            DeclaredError(400, "context_is_frozen",
                          "`provider`/`model` après la déclaration : les changer "
                          "falsifierait l'attribution des lignes déjà écrites"),
            DeclaredError(400, "status_not_settable",
                          "`update status=` — l'état ne se pose pas par une "
                          "retouche de configuration"),
            DeclaredError(400, "field_not_settable",
                          "`update` sur un champ déclaré à la création "
                          "(`procedure`, `project_id`…)"),
            DeclaredError(400, "invalid_bound",
                          "une borne (`workers`, `max_rows`, `max_tokens`…) "
                          "inférieure à 1"),
            DeclaredError(404, "fleet_not_found",
                          "flotte inconnue dans l'org du porteur"),
        ),
        rest=RestBinding(verb="POST", path="/api/me/runner/fleets"),
        description=(
            "Declared configuration of an agent PASS — what a fleet runs, on which "
            "table, within which perimeter, and up to which limit. op=create "
            "(`label` + procedure slug + `tools` allowlist ; optional target "
            "`namespace` + `row_filter`, execution context `provider`/`model`, and "
            "limits `max_rows` / `max_tokens` / `max_consecutive_failures` / "
            "`max_tokens_per_row` — budgets are counted in TOKENS, never money) / "
            "list (optionally filtered by `status`) / get / "
            "state / update. "
            # ⚠️ CE PARAGRAPHE EXISTE PARCE QUE LE NOM DU VERBE INDUIT EN ERREUR
            # TOUT SEUL. « launch » invite à écrire « lance » — trois personnes
            # l'ont annoncé de travers le 02/09/2026, dont un message de tag
            # immuable qui restera faux dans l'historique. Une description d'outil
            # est relue à CHAQUE appel par un modèle qui, lui aussi, lira
            # « launch » et conclura « démarre ». Le texte le plus proche du geste
            # gagne : c'est ici qu'il faut le dire, pas dans une doc à côté.
            "⚠️ op=launch ARMS the fleet — it does NOT start any process. The "
            "state becomes `armed`, never `running`: `running` means a scheduler "
            "has TAKEN it and is beating. A scheduler must still be running for "
            "the pass to move. Symmetrically, op=stop REQUESTS the stop "
            "(`stopping`); the fleet keeps reserving, calling and SPENDING until "
            "the scheduler reads the order and acknowledges it (`stopped`). "
            "Never report a launch on `armed`, nor a stop on `stopping` — the gap "
            "between the two is also the diagnosis: a `stopping` that never "
            "becomes `stopped`, or an `armed` nobody claims, means a dead "
            "scheduler. "
            "op=state returns the pass PROGRESS aggregated "
            "over its jobs — pending, claimed, done, failed, abandoned, tokens "
            "consumed, heaviest single row — and says `no_jobs_attached` "
            "explicitly rather than returning zeros you would read as 'nothing "
            "happened'. The TARGET is frozen at declaration: redirecting a running "
            "pass to another table is what declaring exists to prevent; the "
            "execution context (`provider`/`model`) is frozen too, since changing it "
            "mid-flight falsifies the attribution of rows already written — declare "
            "another fleet instead — duplicate, never switch. The scheduler's own three "
            "verbs are served HERE too: op=take (`armed`→`running`, refused if "
            "another scheduler already took it), op=beat (heartbeat AND reads back "
            "`stop_requested` in the same call) and op=ack_stop "
            "(`stopping`→`stopped`, the only verb that states the FACT of a stop). "
            "They exist so that op=stop is REAL: an order nobody can read is an "
            "order that never happens."
        ),
    ),
]
