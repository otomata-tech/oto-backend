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

import hashlib
import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from . import _cle_exigee, _descriptions_outils, _instruction, _lignes_reservables, _modele
from .. import access, db, output_projection, runner_models, tool_alias
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
    temperature: Optional[float] = None
    workers: Optional[int] = None
    max_rows: Optional[int] = None
    max_tokens: Optional[int] = None
    max_consecutive_failures: Optional[int] = None
    max_tokens_per_row: Optional[int] = None
    descriptions_outils: Optional[dict] = Field(None, description=(
        "What the agent reads of each tool description: {defaut: chars served per "
        "description (≥ 1), entieres: [tools served uncut]}. Frozen at creation; "
        "omitted, the worker's default applies."))
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
    max_rows: Optional[int] = None
    max_tokens: Optional[int] = None
    max_consecutive_failures: Optional[int] = None
    max_tokens_per_row: Optional[int] = None
    descriptions_outils: Optional[dict] = None
    status: Optional[str] = None
    stop_reason: Optional[str] = None
    armed_at: Optional[str] = None
    started_at: Optional[str] = None
    stopping_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    stopped_at: Optional[str] = None
    created_at: Optional[str] = None


class FleetCard(BaseModel):
    """La CARTE d'une flotte, telle que `op=list` la sert : de quoi adresser, trier et
    écarter un passage sans l'ouvrir. La déclaration complète — l'instruction, les
    outils, les bornes de dépense, le contexte d'exécution — se lit par `op=get`.

    `input_sha256` tient lieu d'instruction : deux passages à la même empreinte portent
    le même texte. `null` = aucune instruction.

    ⚠️ Ses champs SONT la projection : le handler garde exactement ceux-ci, et le schéma
    servi les annonce — les deux ne peuvent pas diverger."""
    id: int
    label: Optional[str] = None
    status: Optional[str] = None
    procedure: Optional[str] = None
    namespace: Optional[str] = None
    row_filter: Optional[dict] = None
    max_rows: Optional[int] = None
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    armed_at: Optional[str] = None
    started_at: Optional[str] = None
    stopping_at: Optional[str] = None
    stopped_at: Optional[str] = None
    created_at: Optional[str] = None
    input_sha256: Optional[str] = None


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
    # L'issue des travaux TERMINÉS (oto#243), à côté de `done` : là où l'on regarde.
    empty_jobs: Optional[int] = Field(None, description=(
        "Finished jobs that called `data_claim_next` and got no row."))
    stopped_after_write: Optional[int] = Field(None, description=(
        "Finished jobs stopped by `max_tokens`/`max_steps` after a successful write."))
    reservation_unmeasured: Optional[int] = Field(None, description=(
        "Finished jobs whose run has no reservation count (no run, or opened before "
        "the measure) — never counted as empty."))
    usage_tokens: Optional[int] = None
    heaviest_row_tokens: Optional[int] = None
    usage_unknown: Optional[int] = Field(None, description=(
        "Finished jobs whose `usage_tokens` is unknown: the total leaves them out."))
    reservable_rows: Optional[int] = Field(None, description=(
        "Rows the scheduler still sees as reservable for this pass (at most 15 s "
        "old); `null` with `reservable_rows_unavailable` saying why."))
    reservable_rows_unavailable: Optional[Literal["no_table", "count_failed"]] = None
    last_finished: Optional[str] = None
    no_jobs_attached: bool


class FleetOut(BaseModel):
    fleet: Optional[Fleet] = None
    # `beat` : l'ordre lu dans le même appel que le battement — un ordonnanceur
    # qui bat sans demander « dois-je m'arrêter ? » laisserait `stopping` sans
    # lecteur, et l'arrêt resterait une intention.
    stop_requested: Optional[bool] = None
    beat_taken: Optional[bool] = None
    # `list` : une CARTE par flotte, et `projection` qui NOMME les champs écartés et
    # le verbe qui les rend (`op=get`). `null` sur une liste vide : rien n'a été écarté.
    fleets: Optional[list[FleetCard]] = None
    projection: Optional[dict] = None
    state: Optional[FleetState] = None
    # `launch` : le PIRE CAS du passage, `max_rows × max_tokens_per_row`, dit au
    # moment où l'on engage la dépense. `null` quand une des deux bornes manque —
    # sans borne il n'y a pas de pire cas, et un nombre fabriqué ferait croire à
    # une protection qui n'existe pas. Déclaré ici parce qu'il est SERVI : un
    # champ rendu par le handler et absent du modèle de sortie ne figure dans
    # aucun schéma, donc aucun front ne sait qu'il peut le lire.
    budget_max_tokens: Optional[int] = None


# La phrase servie dans `projection.hint`. ⚠️ Pas l'indice par défaut du seam : il
# prescrit `fields=["*"]`, un paramètre que cette capacité n'a pas — un agent qui le
# suivrait se ferait refuser son appel.
_INDICE_CARTE = ("Carte de tri : `op=get` avec `fleet_id` rend la déclaration complète "
                 "(`input`, `tools`, bornes, contexte d'exécution).")


def _cartes(fleets: list[dict]) -> tuple[list[dict], Optional[dict]]:
    """`op=list` : une CARTE par flotte, et la notice qui nomme ce qu'elle écarte.

    Relevé par l'opérateur des campagnes sur une org réelle : une vingtaine de
    passages, chacun rendu avec son instruction complète (~2 Ko) et son allowlist,
    dans une liste qui ne sert qu'à choisir quoi ouvrir. La projection passe par le
    seam commun, qui NOMME ce qu'il retire ; l'empreinte est prise AVANT, sur le texte
    exact que `get` rend."""
    avec_empreinte = [
        dict(f, input_sha256=(None if f.get("input") is None else
                              hashlib.sha256(f["input"].encode("utf-8")).hexdigest()))
        for f in fleets]
    return output_projection.summarize(
        avec_empreinte, body_fields=(), fields=tuple(FleetCard.model_fields),
        always=("id",), hint=_INDICE_CARTE)


def _noms_canoniques(ctx: ResolvedCtx, inp: FleetInput) -> FleetInput:
    """La déclaration aux noms d'outils CANONIQUES, avant tout le reste (`tool_alias`).

    L'agent d'un tenant déclare ce qu'il voit — `acme_doc`. Le worker, lui, est servi
    en canonique et confronte l'allowlist EXACTEMENT : stockée telle quelle, elle ne
    désignait plus aucun outil, et le passage tournait sans. La consigne (`input`) et
    les outils servis entiers (`descriptions_outils.entieres`) suivent la même règle —
    ceux-ci doivent rester un sous-ensemble de `tools`, donc dans la même langue."""
    maj: dict[str, Any] = {}
    if inp.tools is not None:
        maj["tools"] = tool_alias.canonical_names(inp.tools, ctx.sub)
    if inp.input:
        maj["input"] = tool_alias.canonical_prose(inp.input, ctx.sub)
    reglage = inp.descriptions_outils
    if isinstance(reglage, dict) and isinstance(reglage.get("entieres"), list):
        maj["descriptions_outils"] = {
            **reglage, "entieres": tool_alias.canonical_names(reglage["entieres"], ctx.sub)}
    return inp.model_copy(update=maj) if maj else inp


def _fleets(ctx: ResolvedCtx, inp: FleetInput) -> dict:
    if not ctx.org_id:
        raise AuthzDenied(400, "org_required", "les flottes sont org-scopées")
    inp = _noms_canoniques(ctx, inp)
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
        # ⚠️ Le modèle d'un passage PART désormais avec ses travaux (12/09/2026) —
        # il ne peut donc plus être une chaîne libre : un nom que rien ne route
        # promettrait une attribution fausse. `provider` ne choisit plus rien, il
        # se déduit du modèle ; fourni, il doit le confirmer.
        famille = _modele.famille_declaree(inp.model, inp.provider)
        # Un runner armé ne suffit pas quand l'org doit tourner sur SA clé : sans
        # elle, le travail serait arrêté à la réservation. La création est l'un
        # des trois moments de POSE (module `_cle_exigee`) : le dire ICI, avant
        # même qu'un passage existe, plutôt qu'à son armement seulement. Seule la
        # famille DE CE MODÈLE compte (14/09/2026) — un passage sans modèle
        # n'exige rien.
        _cle_exigee.exiger_a_la_pose(ctx.org_id, famille)
        descriptions = _descriptions_outils.valider(inp.descriptions_outils, inp.tools)
        return {"fleet": db.create_fleet(
            ctx.org_id, ctx.sub, label=inp.label, procedure=inp.procedure,
            tools=inp.tools, namespace=inp.namespace, row_filter=inp.row_filter,
            project_id=inp.project_id, max_steps=inp.max_steps,
            input=inp.input or _instruction.de_file(
                inp.procedure, inp.namespace, inp.row_filter),
            provider=famille, model=inp.model or None,
            temperature=inp.temperature, workers=inp.workers or 1,
            max_rows=inp.max_rows, max_tokens=inp.max_tokens,
            max_consecutive_failures=inp.max_consecutive_failures,
            max_tokens_per_row=inp.max_tokens_per_row,
            descriptions_outils=descriptions)}

    if inp.op == "list":
        cartes, projection = _cartes(db.list_fleets(ctx.org_id, inp.status))
        return {"fleets": cartes, "projection": projection}

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
        famille = runner_models.famille((avant or {}).get("model"))
        # ⚠️ Avant d'armer, et avant la réparation de l'instruction : un refus
        # n'écrit rien. Armé sans la clé exigée, le passage passerait `running` au
        # premier travail — arrêté aussitôt à la réservation — puis au suivant.
        # Seule la famille DE CE MODÈLE compte (14/09/2026) — un passage sans
        # modèle (ou d'un modèle hors catalogue) n'exige rien.
        _cle_exigee.exiger_a_la_pose(ctx.org_id, famille)
        # ⚠️ Armer un passage dont AUCUN worker vivant ne sert le modèle le laisse
        # `running` pour toujours : `campagne_a_servir` produit un travail, le
        # claim le filtre, et plus rien n'est produit tant qu'il attend. Refusé
        # AVANT la réparation de l'instruction — un refus n'écrit rien.
        # Un passage sans modèle (ou d'un modèle hors catalogue) n'est pas jugé :
        # n'importe quel worker le sert.
        if famille:
            _modele.exige_servi(db.runner_arme(ctx.org_id), famille)
        if avant and avant.get("procedure") and not (avant.get("input") or "").strip():
            db.update_fleet(inp.fleet_id, ctx.org_id, {"input": _instruction.de_file(
                avant["procedure"], avant.get("namespace"), avant.get("row_filter"))})
        # ⚠️ UN SEUL armement, APRÈS la réparation : l'ordre que #873 existe pour tenir.
        # Il ne compte plus les lignes visées (13/09/2026) : ce compte ne voyait que le
        # `row_filter`, pas le périmètre déclaré du tableau, et annonçait du travail
        # qu'aucune réservation ne servait. La plateforme ne compte pas à la place de
        # l'agent, qui découvre une file vide en réservant.
        # ⚠️ La borne se vérifie ICI aussi, et sur la flotte EN BASE — pas sur
        # l'entrée. Une campagne déclarée avant cette garde, ou modifiée depuis,
        # n'a rien qui l'arrête ; et c'est l'armement qui engage la dépense, pas
        # la déclaration. Le refus nomme sa destination : `op=update`.
        f = db.armer(inp.fleet_id, ctx.org_id)
        if not f:
            actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
            if not actuelle:
                raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
            raise AuthzDenied(
                409, "not_launchable",
                f"ce passage est `{actuelle['status']}` — on n'arme que ce qui ne "
                "tourne pas. Arrête-le d'abord, ou déclare une autre flotte.")
        # ⚠️ Le PIRE CAS, dit au moment où on engage — pas à lire dans une doc.
        # `max_rows` borne un nombre de travaux et `max_tokens_per_row` ce qu'un
        # travail peut coûter : leur produit est la dépense maximale du passage,
        # et c'est le seul chiffre qui répond à « combien ça peut coûter ». Sans
        # `max_rows`, il n'y a pas de pire cas — et le dire vaut mieux que de
        # rendre un nombre qui ne borne rien.
        lignes, par_ligne = f.get("max_rows"), f.get("max_tokens_per_row")
        return {"fleet": f,
                "budget_max_tokens": (int(lignes) * int(par_ligne)
                                      if lignes and par_ligne else None)}

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
        # La file telle que l'ordonnanceur la voit, pour qui SUPERVISE la campagne
        # (14/09/2026) — jamais pour l'agent qui travaille (`_lignes_reservables`).
        etat["state"].update(_lignes_reservables.pour_le_superviseur(etat["fleet"]))
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
    if inp.descriptions_outils is not None:
        raise AuthzDenied(
            400, "context_is_frozen",
            "`descriptions_outils` ne se modifie pas — ce que l'agent lit des outils est "
            "du contexte d'exécution, figé à la déclaration comme le modèle. Le changer "
            "en vol rendrait incomparables les lignes déjà écrites. Déclare une autre flotte.")
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
    # `tools` reste modifiable, `descriptions_outils` non : une allowlist qui retirerait un
    # outil que le réglage nomme le rendrait inerte sans un mot (oto#241).
    if inp.tools is not None:
        actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
        retires = _descriptions_outils.outils_nommes_retires(
            (actuelle or {}).get("descriptions_outils"), inp.tools)
        if retires:
            raise AuthzDenied(
                400, _descriptions_outils.CODE,
                f"`tools` retirerait {', '.join(retires)}, que `descriptions_outils."
                "entieres` nomme : le réglage de cette flotte cesserait d'agir. Garde ces "
                "outils, ou déclare une autre flotte.")
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
            DeclaredError(400, "invalid_model",
                          "`create` avec un `model` hors catalogue, un `provider` "
                          "qui le contredit, ou un `provider` sans `model`"),
            DeclaredError(400, "model_not_served",
                          "`launch` d'un passage dont aucun worker vivant ne sert "
                          "la famille du modèle"),
            DeclaredError(400, "model_key_required",
                          "`launch` d'un passage dans une org qui doit tourner sur SA "
                          "clé de modèle et ne l'a pas déposée"),
            DeclaredError(404, "fleet_not_found",
                          "flotte inconnue dans l'org du porteur"),
        ),
        rest=RestBinding(verb="POST", path="/api/me/runner/fleets"),
        description=(
            "Declared configuration of an agent PASS — what a fleet runs, on which "
            "table, within which perimeter, and up to which limit. op=create "
            "(`label` + procedure slug + `tools` allowlist ; optional target "
            "`namespace` + `row_filter`, execution context `model` — one of the "
            "catalogue served as `runner.models` by oto_trigger; `provider` is "
            "deduced from it; omitted, the worker runs its own —, and "
            "limits `max_rows` / `max_tokens` / `max_consecutive_failures` / "
            "`max_tokens_per_row` — budgets are counted in TOKENS, never money) / "
            "list (optionally filtered by `status`; one CARD per fleet — `input_sha256` "
            "in place of `input`, and `projection` names what it leaves out) / get (the "
            "full declaration) / "
            "state / update. "
            # ⚠️ CE PARAGRAPHE EXISTE PARCE QUE LE NOM DU VERBE INDUIT EN ERREUR
            # TOUT SEUL. « launch » invite à écrire « lance » — trois personnes
            # l'ont annoncé de travers le 02/09/2026, dont un message de tag
            # immuable qui restera faux dans l'historique. Une description d'outil
            # est relue à CHAQUE appel par un modèle qui, lui aussi, lira
            # « launch » et conclura « démarre ». Le texte le plus proche du geste
            # gagne : c'est ici qu'il faut le dire, pas dans une doc à côté.
            "⚠️ op=launch ARMS the fleet — it does NOT start any process. The "
            "state becomes `armed`, never `running`: `running` is a FACT, not an "
            "intent — a worker asked for work and Oto produced this pass's first "
            "job. That happens BY ITSELF, usually within seconds: workers poll "
            "continuously and Oto makes the work when they ask. **No scheduler "
            "is involved and none has to be started.** An `armed` still `armed` "
            "after a minute therefore means NO WORKER IS POLLING for this org — "
            "not 'nobody has taken it'. Symmetrically, op=stop REQUESTS the stop "
            "(`stopping`); the fleet keeps reserving, calling and SPENDING until "
            "none of its jobs is left in flight, at which point Oto states the "
            "fact (`stopped`) at the next poll. "
            "Never report a launch on `armed`, nor a stop on `stopping` — the gap "
            "between the two is also the diagnosis. op=launch is REFUSED "
            "(`model_not_served`) when the fleet declares a model no live worker "
            "serves: its jobs would wait forever. "
            "op=state returns the pass PROGRESS aggregated "
            "over its jobs — pending, claimed, done, failed, abandoned, tokens "
            "consumed, heaviest single row, `empty_jobs` (finished with no row), "
            "`stopped_after_write` — plus `reservable_rows`, what the scheduler still "
            "sees to serve — and says `no_jobs_attached` "
            "explicitly rather than returning zeros you would read as 'nothing "
            "happened'. The TARGET is frozen at declaration: redirecting a running "
            "pass to another table is what declaring exists to prevent; the "
            "execution context (`provider`/`model`) is frozen too, since changing it "
            "mid-flight falsifies the attribution of rows already written — declare "
            "another fleet instead — duplicate, never switch. An EXTERNAL scheduler "
            "is still served here — op=take (`armed`→`running`, refused if another "
            "scheduler already took it), op=beat (heartbeat AND reads back "
            "`stop_requested` in the same call), op=ack_stop (`stopping`→`stopped`). "
            "⚠️ None of them is required any more, and you should not call them: "
            "polling alone moves a pass, and op=take would only claim one that was "
            "about to start on its own."
        ),
    ),
]
