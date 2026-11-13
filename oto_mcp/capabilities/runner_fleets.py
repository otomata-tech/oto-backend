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
Depuis le 21/09/2026 ils nomment leur auteur (`taken_by`) et ne se font que par
l'ordonnanceur qui TIENT la campagne — `_ordonnanceur_de_campagne.py`.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from . import (_abonnement, _cle_exigee, _descriptions_outils, _instruction,
               _limites_du_run, _lignes_de_campagne, _lignes_reservables, _modele,
               _ordonnanceur_de_campagne, _outils_manquants, _outils_plateforme)
from .. import (db, deprecations, output_projection, runner_models, session_org,
               tool_alias, verrou_org)

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
    Il vit dans le contexte d'appel, posé par l'axe `_run_id` — et SEULEMENT par
    lui, PAR CHOIX : le middleware pose aussi le run actif de la pile de session
    (`session_org.current_session_run`), que ces gardes ne lisent PAS (#830) — un
    refus anti-agent se décide sur le run que l'appel NOMME, comme l'org du run
    (#639). `test_run_de_session_830.py` rougit si on les y branche. L'axe
    n'atteint `oto_fleet` que parce qu'il figure dans `call_axes._RUN_SPINE_TOOLS` ;
    l'en retirer rendrait ces gardes inertes sans qu'aucun banc à doublure ne
    rougisse (`test_garde_anti_boucle_flotte_830.py` passe par le vrai middleware).
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
    # Une DURÉE, pas un compte : sa fourchette est celle de l'exécuteur, et `0` la retire.
    _limites_du_run.valider(None, inp.max_run_seconds)


class FleetInput(BaseModel):
    op: Literal["create", "list", "get", "state", "update", "launch", "stop",
                "take", "beat", "ack_stop"]
    fleet_id: Optional[int] = None
    status: Optional[str] = None
    # create —
    label: Optional[str] = None
    procedure: Optional[str] = None
    tools: Optional[list[str]] = None
    namespace: Optional[str] = Field(None, description=(
        "create: the target table, by id or name. Resolved ONCE, at declaration, in "
        "the declarer's scope, and kept as the table's id — served back in "
        "`namespace`. A name that several tables carry at the same rank is refused "
        "(`datastore_ambigu`)."))
    row_filter: Optional[dict] = None
    project_id: Optional[int] = None
    input: Optional[str] = None
    max_steps: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    workers: Optional[int] = Field(None, description=(
        "Cap on this campaign's jobs in flight (pending + claimed), default 1. A "
        "ceiling, not a head count: the runners polling the queue decide how many "
        "actually run (see `runner.workers` on triggers)."))
    max_rows: Optional[int] = None
    max_tokens: Optional[int] = None
    max_consecutive_failures: Optional[int] = None
    max_tokens_per_row: Optional[int] = None
    max_run_seconds: Optional[int] = Field(None, description=(
        "Per-row time limit, 60-3600 s. `0` = runner default."))
    descriptions_outils: Optional[dict] = Field(None, description=(
        "What the agent reads of each tool description: {defaut: chars served per "
        "description (≥ 1), entieres: [tools served uncut]}. Frozen at creation; "
        "omitted, the worker's default applies."))
    # stop — la raison est ÉCRITE : « arrêtée » sans raison oblige à rouvrir les
    # journaux pour savoir si c'était un incident, un budget ou une décision.
    reason: Optional[str] = None
    # take / beat / ack_stop — QUI tient la campagne (21/09/2026).
    taken_by: Optional[str] = Field(None, max_length=200, description=(
        "take/beat/ack_stop (required): the scheduler's own identifier — stable across "
        "its restarts, distinct from any other scheduler."))


# Domaine fermé par le CHECK `runner_fleets_status_check` (#437).
FleetStatus = Literal["draft", "armed", "running", "stopping", "stopped", "done",
                      "failed"]


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
    #: L'IDENTIFIANT du tableau visé, résolu à la déclaration (#1067) ; un nom = une
    #: automatisation d'avant, fixée au premier armement ou travail.
    namespace: Optional[str] = None
    row_filter: Optional[dict] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    #: DÉCLARÉ : le plafond de travaux en cours de la campagne (#907, oto#245) —
    #: jamais le nombre d'exécutants, que sert `RunnerArme.workers` (constaté).
    workers: Optional[int] = Field(None, description=(
        "Declared cap on jobs in flight for this campaign — not the number of "
        "running agents (that measured count is `runner.workers` on triggers)."))
    max_rows: Optional[int] = None
    max_tokens: Optional[int] = None
    max_consecutive_failures: Optional[int] = None
    max_tokens_per_row: Optional[int] = None
    max_run_seconds: Optional[int] = None
    descriptions_outils: Optional[dict] = None
    status: Optional[FleetStatus] = None
    stop_reason: Optional[str] = None
    armed_at: Optional[str] = None
    started_at: Optional[str] = None
    stopping_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    # L'ordonnanceur qui TIENT la campagne, tel qu'il s'est déclaré à `op=take` ;
    # `null` = personne ne la tient.
    taken_by: Optional[str] = None
    stopped_at: Optional[str] = None
    created_at: Optional[str] = None
    trigger_id: Optional[int] = Field(None, description=(
        "The hosted agent (`oto_trigger`) whose run declared this pass — null when a "
        "person declared it, or for a pass declared before 23/10/2026."))


class FleetCard(BaseModel):
    """La CARTE d'une flotte, telle que `op=list` la sert : de quoi adresser, trier et
    écarter un passage sans l'ouvrir. La déclaration complète — l'instruction, les
    outils, les bornes de dépense, le contexte d'exécution — se lit par `op=get`.

    `input_sha256` tient lieu d'instruction : deux passages à la même empreinte portent
    le même texte. `null` = aucune instruction. `taken_by` dit quel ordonnanceur tient
    la campagne (`null` = aucun) — ce qu'un ordonnanceur qui redémarre lit pour savoir
    si elle est la sienne.

    ⚠️ Ses champs SONT la projection : le handler garde exactement ceux-ci, et le schéma
    servi les annonce — les deux ne peuvent pas diverger."""
    id: int
    label: Optional[str] = None
    status: Optional[FleetStatus] = None
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
    taken_by: Optional[str] = None
    trigger_id: Optional[int] = None
    input_sha256: Optional[str] = None


class AbandonReason(BaseModel):
    reason: Optional[str] = Field(None, description=(
        "The reason the platform recorded when it took the row out of the queue — it "
        "ends with what the last attempt hit (`— last attempt: …`), so rows "
        "group by cause. null: the row was put in the abandon state by a write, not "
        "by the platform."))
    rows: int


class FleetRows(BaseModel):
    """Ce que sont devenues les LIGNES d'une automatisation (oto#77) — lues au
    tableau, par la valeur finale de leur colonne de statut. Les compteurs de
    travaux n'en disent rien : un travail qui rend sa ligne sans l'écrire est `done`
    (`capabilities/_lignes_de_campagne.py`)."""
    scope: Literal["perimeter", "table"] = Field(description=(
        "`perimeter`: the rows the automation's `row_filter` designates, its status "
        "clause removed. `table`: the filter bounds only the status, so these are ALL "
        "the rows of the table — this automation's rows cannot be told apart."))
    status_column: str
    perimeter: dict = Field(description="The filter these rows were counted under.")
    total: int
    by_status: dict[str, int] = Field(description=(
        "Rows per final value of the status column, as written. `(none)` = no value."))
    terminal_states: list[str]
    abandon_state: Optional[str] = None
    concluded: Optional[int] = Field(None, description=(
        "Rows in a terminal state other than the abandon state — whether that means "
        "done or discarded is in `by_status`. null: no terminal state is declared."))
    abandoned: Optional[int] = Field(None, description=(
        "Rows in the abandon state: they FAILED and left the queue. null: the "
        "lifecycle declares no abandon state."))
    open: Optional[int] = Field(None, description=(
        "Rows not in a terminal state yet. null: no terminal state is declared."))
    abandon_reasons: list[AbandonReason] = Field(description=(
        "Why the abandoned rows were abandoned, grouped by reason."))


class FleetState(BaseModel):
    """L'avancement d'un passage : ses TRAVAUX, ses RUNS et ses LIGNES — trois
    comptes qui ne se déduisent pas l'un de l'autre (oto#77).

    `no_jobs_attached` est DÉCLARÉ plutôt que déduit de compteurs à zéro :
    un zéro qui peut vouloir dire « rien trouvé » ou « personne n'a regardé » est
    le défaut qui a coûté le plus cher sur ce chantier.
    """
    jobs_total: int
    pending: Optional[int] = None
    claimed: Optional[int] = None
    done: Optional[int] = Field(None, description=(
        "JOBS that ended by themselves. Says nothing about the rows: a job that gives "
        "its row back unwritten still ends `done` — read `rows`."))
    failed: Optional[int] = Field(None, description=(
        "JOBS that errored (a row never counts here — see `rows.abandoned`)."))
    abandoned: Optional[int] = Field(None, description=(
        "JOBS that errored with no attempt left (a row never counts here — see "
        "`rows.abandoned`)."))
    runs_by_outcome: Optional[dict[str, int]] = Field(None, description=(
        "Runs of these jobs by the outcome the agent declared at `run_finish` (done, "
        "partial, failed, blocked); `open` = not finished, `unknown` = no run start "
        "on record."))
    rows: Optional[FleetRows] = None
    rows_unavailable: Optional[Literal["no_table", "table_not_found", "table_ambiguous",
                                       "no_status_column"]] = Field(None, description=(
        "Why `rows` is null: `no_table` (no target), `table_not_found` (the table is "
        "gone or out of the declarer's reach), `table_ambiguous` (a pass declared "
        "before its table was kept by id, whose name no longer designates one table), "
        "`no_status_column`."))
    # L'issue des travaux TERMINÉS (oto#243), à côté de `done` : là où l'on regarde.
    empty_jobs: Optional[int] = Field(None, description=(
        "Finished jobs that called `data_claim_next` and got no row."))
    stopped_after_write: Optional[int] = Field(None, description=(
        "Finished jobs stopped by `max_tokens`/`max_steps`/`max_seconds` after a successful write."))
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


def _cible_a_la_declaration(ctx: ResolvedCtx, adresse: Optional[str]) -> Optional[str]:
    """La cible d'une automatisation, résolue UNE fois ici et gardée par son
    IDENTIFIANT (#1067) — `None` sans cible. Un nom se résout dans la portée du
    déclarant (`_lignes_reservables.cle_a_la_declaration`) : ce qui lit l'automatisation
    ensuite passe par l'identifiant, et un homonyme apparu plus tard n'y change rien.
    Rien ne répond → 404 ; plusieurs tableaux à ce nom au même rang → 409 nommé."""
    adresse = (adresse or "").strip()
    if not adresse:
        return None
    try:
        cle = _lignes_reservables.cle_a_la_declaration(
            adresse, sub=ctx.sub, org_id=ctx.org_id)
    except _lignes_reservables.TableauAmbigu as e:
        raise AuthzDenied(409, "datastore_ambigu", str(e)) from None
    if cle is None:
        raise AuthzDenied(
            404, "datastore_not_found",
            f"aucun tableau « {adresse} » dans ta portée : une automatisation vise un "
            "tableau qui existe, désigné par son identifiant.")
    if deprecations.adresse_par_nom(adresse, cle):
        session_org.noter_avis(deprecations.avis_nom_de_tableau(adresse, cle))
    return str(cle)


def _agent_declarant(org_id: int) -> Optional[int]:
    """L'agent hébergé dont le run DÉCLARE ce passage — None pour une personne.

    Lu dans le travail du JETON (`verrou_org.travail_de_la_requete`), jamais dans un
    argument de l'appel : un agent n'a pas à dire qui il est, et ne peut pas se ranger
    sous un autre. L'agent est celui que le SERVEUR a posé dans la charge de ce travail
    (`db.agent_du_travail` ; `runner.jobs enqueue` refuse un `trigger_id` fourni). Un
    agent d'une autre org, ou supprimé depuis, ne se pose pas.

    Pourquoi : l'écran des agents range un passage sous l'agent qui l'a lancé, même
    quand ce passage joue une AUTRE procédure que la sienne — l'appariement par
    procédure le montrait comme une campagne lancée à la main."""
    v = verrou_org.travail_de_la_requete()
    if v is None or v.job_id is None:
        return None
    return db.agent_du_travail(v.job_id, org_id)


def _fleets(ctx: ResolvedCtx, inp: FleetInput) -> dict:
    if not ctx.org_id:
        raise AuthzDenied(400, "org_required", "les automatisations sont org-scopées")
    inp = _noms_canoniques(ctx, inp)
    # Ouvertes à toute org depuis le 24/09/2026 : plus de porte `beta`. Ce qui
    # borne la dépense, c'est le modèle OBLIGATOIRE (`_modele.exige_un_modele`) et
    # la clé de modèle de l'org (`_cle_exigee`), à la pose comme à la réservation.

    if inp.op == "create":
        # ⚠️ Le seam vaut pour TOUTE opération, pas pour le seul verbe qu'on avait
        # regardé. `create status="running"` rendait 200 avec une flotte `draft` et
        # le champ avalé — mot pour mot le geste que le refus d'`update` prédit.
        # Une garde écrite dans une branche ne garde que cette branche.
        if inp.status is not None:
            raise AuthzDenied(
                400, "status_not_settable",
                "l'état d'une automatisation ne se pose pas à la création — elle naît "
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
                f"create exige : {', '.join(manquants)} — le nom de l'automatisation, "
                "la procédure à jouer, et les outils (l'allowlist de ses exécutions)")
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
        _modele.exige_region_permise(ctx.org_id, inp.model)
        # Le modèle est OBLIGATOIRE (24/09/2026) : sans lui, le passage tournerait
        # sur le modèle du worker — notre clé — et aucune clé d'org ne serait exigée.
        _modele.exige_un_modele(famille)
        # Un runner armé ne suffit pas quand l'org doit tourner sur SA clé : sans
        # elle, le travail serait arrêté à la réservation. La création est l'un
        # des trois moments de POSE (module `_cle_exigee`) : le dire ICI, avant
        # même qu'un passage existe, plutôt qu'à son armement seulement. Seule la
        # famille DE CE MODÈLE compte (14/09/2026) — un passage sans modèle
        # n'exige rien.
        _cle_exigee.exiger_a_la_pose(ctx.org_id, famille)
        # ⚠️ Un modèle d'ABONNEMENT ne se pose sur une flotte QUE si l'org tourne en
        # mode pool (OTO-130) : un passage appartient à l'organisation, et hors pool
        # il ferait payer le forfait d'une personne pour le travail de tous. Refusé
        # à la création, là où on peut encore choisir un modèle servi par une clé
        # d'organisation ; en pool, il faut qu'un membre y prête un abonnement.
        _abonnement.exiger_a_la_pose(ctx.sub, None, famille, flotte=True,
                                     org_id=ctx.org_id)
        descriptions = _descriptions_outils.valider(inp.descriptions_outils, inp.tools)
        cible = _cible_a_la_declaration(ctx, inp.namespace)
        return {"fleet": db.create_fleet(
            ctx.org_id, ctx.sub, label=inp.label, procedure=inp.procedure,
            tools=inp.tools, namespace=cible, row_filter=inp.row_filter,
            project_id=inp.project_id, max_steps=inp.max_steps,
            input=inp.input or _instruction.de_file(
                inp.procedure, cible, inp.row_filter),
            provider=famille, model=inp.model or None,
            temperature=inp.temperature, workers=inp.workers or 1,
            max_rows=inp.max_rows, max_tokens=inp.max_tokens,
            max_consecutive_failures=inp.max_consecutive_failures,
            max_tokens_per_row=inp.max_tokens_per_row,
            max_run_seconds=_limites_du_run.a_ecrire(inp.max_run_seconds),
            descriptions_outils=descriptions,
            trigger_id=_agent_declarant(ctx.org_id))}

    if inp.op == "list":
        cartes, projection = _cartes(db.list_fleets(ctx.org_id, inp.status))
        return {"fleets": cartes, "projection": projection}

    if inp.fleet_id is None:
        raise AuthzDenied(400, "missing_fields", f"{inp.op} exige `fleet_id`")

    if inp.op == "get":
        f = db.get_fleet(inp.fleet_id, ctx.org_id)
        if not f:
            raise AuthzDenied(404, "fleet_not_found", "automatisation inconnue")
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
                "lancer une automatisation est réservé aux administrateurs de l'org : il "
                "engage une dépense et des écritures chez un tiers. L'ARRÊTER, en "
                "revanche, est ouvert à tout membre.")
        # ⚠️ Un déroulé ne LANCE pas. Un agent qui se relance lui-même coûte un
        # budget en boucle — pire qu'un arrêt de trop, qui ne coûte qu'une reprise.
        if _run_courant():
            raise AuthzDenied(
                403, "not_from_a_run",
                "un run ne lance pas d'automatisation — un agent qui se relance "
                "lui-même dépense en boucle.")
        # ⚠️ Une campagne déclarée avant que la plateforme compose — ou par une
        # surface qui a laissé le champ vide — n'a pas d'instruction. L'armer
        # telle quelle la rend MUETTE au premier passage : le worker refuse de
        # démarrer sans instruction, la flotte reste `armed`, et le symptôme lu
        # depuis le produit est « l'ordonnanceur est mort » — un diagnostic faux
        # posé sur une cause invisible. On répare AVANT d'armer, jamais après.
        avant = db.get_fleet(inp.fleet_id, ctx.org_id)
        # ⚠️ Un refus qui NOMME, avant d'armer (incident du 22/09) : la boîte
        # d'un worker dépend de connecteurs exposés + sélectionnés dans l'org
        # visée (ADR 0011/0019), pas seulement de la liste déclarée. Un manque
        # ici n'écrivait rien au moment où ça comptait — l'agent ne le disait
        # que dans ses notes de sortie, après coup.
        if avant:
            manquants = _outils_manquants.manquants(
                ctx.org_id, avant.get("sub"), avant.get("tools"))
            if manquants:
                raise AuthzDenied(
                    409, "tools_not_mounted",
                    "cette automatisation déclare des outils absents de la boîte de l'org "
                    f"visée : {', '.join(sorted(manquants))} — installe-les "
                    "(`oto_connector op=select`) ou retire-les de `tools` "
                    "(`op=update`) avant de lancer.")
        famille = runner_models.famille((avant or {}).get("model"))
        # Un passage déclaré sans modèle avant le 24/09/2026 ne s'arme plus : il
        # tournerait sur le modèle du worker. Le modèle étant figé à la déclaration,
        # la sortie est d'en déclarer un autre. (Passage inconnu : le 404 vient plus
        # loin, ce refus ne doit pas le masquer.)
        if avant:
            _modele.exige_un_modele(famille)
        # ⚠️ Avant d'armer, et avant la réparation de l'instruction : un refus
        # n'écrit rien. Armé sans la clé exigée, le passage passerait `running` au
        # premier travail — arrêté aussitôt à la réservation — puis au suivant.
        # Seule la famille DE CE MODÈLE compte (14/09/2026) — un passage sans
        # modèle (ou d'un modèle hors catalogue) n'exige rien.
        _cle_exigee.exiger_a_la_pose(ctx.org_id, famille)
        # Un passage déclaré avant cette garde (ou dont le modèle a changé de
        # nature) ne s'arme pas non plus sur un abonnement personnel.
        _abonnement.exiger_a_la_pose(ctx.sub, None, famille, flotte=True,
                                     org_id=ctx.org_id)
        # ⚠️ Armer un passage qu'AUCUN worker vivant ne réclame le laisse `armed`
        # pour toujours : personne ne fait jamais `prendre_flotte`, et le
        # symptôme lu depuis le produit est « l'ordonnanceur est mort » — un
        # diagnostic faux posé sur une cause invisible (oto-runner#13, 41
        # travaux restés en file 13 jours). Lu même SANS modèle demandé : un
        # agent sans modèle est servi par n'importe quel worker, encore faut-il
        # qu'il y en ait un. L'état lu sert aussi à la garde de famille qui suit.
        etat = _modele.exige_un_runner(ctx.org_id)
        # ⚠️ Armer un passage dont AUCUN worker vivant ne sert le modèle le laisse
        # `running` pour toujours : `campagne_a_servir` produit un travail, le
        # claim le filtre, et plus rien n'est produit tant qu'il attend. Un
        # passage sans modèle (ou d'un modèle hors catalogue) n'est pas jugé :
        # n'importe quel worker le sert.
        if famille:
            _modele.exige_servi(etat, famille)
        # ⚠️ La borne `max_rows` se vérifie ICI aussi, sur la flotte EN BASE, avant
        # toute écriture. Une campagne close à sa borne (`done`, `max_rows`) se
        # réarmerait sans erreur, mais `campagne_a_servir` ne lui produirait plus
        # rien : elle resterait `armed` en silence. Le compte ne repart pas de
        # zéro au réarmement, le refus nomme donc les deux sorties.
        if avant and avant.get("max_rows"):
            produits = db.travaux_produits(inp.fleet_id, ctx.org_id)
            if produits >= int(avant["max_rows"]):
                raise AuthzDenied(
                    409, "max_rows_reached",
                    f"cette automatisation a déjà produit {produits} exécutions pour "
                    f"`max_rows`={avant['max_rows']} : armée, elle n'en produirait plus "
                    "aucune. Relève `max_rows` par `op=update`, ou déclare une autre "
                    "automatisation.")
        # Une automatisation d'avant #1067 garde le NOM de son tableau : on pose son
        # identifiant avant d'armer, là où un refus n'a encore rien engagé.
        if avant:
            try:
                avant = _lignes_reservables.fixer_le_tableau(avant)
            except _lignes_reservables.TableauAmbigu as e:
                raise AuthzDenied(409, "datastore_ambigu", str(e)) from None
        if avant and avant.get("procedure") and not (avant.get("input") or "").strip():
            db.update_fleet(inp.fleet_id, ctx.org_id, {"input": _instruction.de_file(
                avant["procedure"], avant.get("namespace"), avant.get("row_filter"))})
        # ⚠️ UN SEUL armement, APRÈS la réparation : l'ordre que #873 existe pour tenir.
        # Il ne compte plus les lignes visées (13/09/2026) : ce compte ne voyait que le
        # `row_filter`, pas le périmètre déclaré du tableau, et annonçait du travail
        # qu'aucune réservation ne servait. La plateforme ne compte pas à la place de
        # l'agent, qui découvre une file vide en réservant.
        f = db.armer(inp.fleet_id, ctx.org_id)
        if not f:
            actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
            if not actuelle:
                raise AuthzDenied(404, "fleet_not_found", "automatisation inconnue")
            raise AuthzDenied(
                409, "not_launchable",
                f"cette automatisation est `{actuelle['status']}` — on n'arme que ce qui "
                "ne tourne pas. Arrête-la d'abord, ou déclare une autre automatisation.")
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
                "un run ne peut pas arrêter l'automatisation qui l'exécute.")
        f = db.demander_arret(inp.fleet_id, ctx.org_id,
                              inp.reason or "arrêt demandé")
        if not f:
            actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
            if not actuelle:
                raise AuthzDenied(404, "fleet_not_found", "automatisation inconnue")
            raise AuthzDenied(
                409, "not_stoppable",
                f"cette automatisation est `{actuelle['status']}` — il n'y a rien à arrêter.")
        # ⚠️ `stopping`, pas `stopped` : l'ordre est POSÉ, la boucle ne l'a pas
        # encore lu. Le passage continue jusqu'à ce qu'elle accuse réception.
        return {"fleet": f}

    # ── Les gestes de l'ORDONNANCEUR — ceux qui transforment une intention en
    # fait, et que seul celui qui TIENT la campagne peut faire (21/09/2026).
    if inp.op in _ordonnanceur_de_campagne.GESTES:
        return _ordonnanceur_de_campagne.geste(ctx.org_id, inp.op, inp.fleet_id,
                                               inp.taken_by, inp.reason)

    if inp.op == "state":
        etat = db.fleet_state(inp.fleet_id, ctx.org_id)
        if not etat:
            raise AuthzDenied(404, "fleet_not_found", "automatisation inconnue")
        # La file telle que l'ordonnanceur la voit, pour qui SUPERVISE la campagne
        # (14/09/2026) — jamais pour l'agent qui travaille (`_lignes_reservables`).
        etat["state"].update(_lignes_reservables.pour_le_superviseur(etat["fleet"]))
        # Ce que sont devenues les LIGNES (oto#77) : sans elles, onze travaux `done`
        # se lisaient comme un succès sur deux lignes abandonnées sur trois.
        etat["state"].update(_lignes_de_campagne.pour_le_superviseur(etat["fleet"]))
        return etat

    # update — partiel, et jamais sur la cible ni sur l'état.
    champs: dict[str, Any] = {c: getattr(inp, c) for c in db.CHAMPS_MODIFIABLES
                              if getattr(inp, c) is not None}
    if "max_run_seconds" in champs:
        champs["max_run_seconds"] = _limites_du_run.a_ecrire(champs["max_run_seconds"])
    if inp.namespace is not None or inp.row_filter is not None:
        raise AuthzDenied(
            400, "target_is_frozen",
            "la cible d'une automatisation ne se modifie pas — `namespace` et `row_filter` "
            "sont figés à la déclaration. Un autre tableau, c'est une autre automatisation.")
    if inp.provider is not None or inp.model is not None:
        raise AuthzDenied(
            400, "context_is_frozen",
            "le contexte d'exécution ne se modifie pas — `provider` et `model` sont "
            "figés à la déclaration. Les changer en vol rendrait FAUSSE l'attribution "
            "des lignes déjà écrites sous cette automatisation. Déclares-en une autre.")
    if inp.descriptions_outils is not None:
        raise AuthzDenied(
            400, "context_is_frozen",
            "`descriptions_outils` ne se modifie pas — ce que l'agent lit des outils est "
            "du contexte d'exécution, figé à la déclaration comme le modèle. Le changer "
            "en vol rendrait incomparables les lignes déjà écrites. Déclare une autre automatisation.")
    # ⚠️ `status` figure dans l'entrée parce qu'il FILTRE `list`. Le laisser tomber
    # en silence ici rendrait 200 avec la flotte inchangée — et c'est précisément le
    # geste qu'un agent privé de `stop` tenterait, en lisant un succès dans la
    # réponse. Un vide ne doit jamais se lire comme un fait.
    if inp.status is not None:
        raise AuthzDenied(
            400, "status_not_settable",
            "l'état d'une automatisation ne se pose pas par `update` — `status` ne sert "
            "ici qu'à FILTRER `list`. Armer une automatisation, c'est `op=launch` ; "
            "demander son arrêt, `op=stop`.")
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
            "l'exige. Une automatisation sans outils n'exécute rien.")
    fournis = {c for c, v in inp.model_dump(exclude_none=True).items()
               if c not in _STRUCTURELS}
    inertes = sorted(fournis - set(db.CHAMPS_MODIFIABLES))
    if inertes:
        raise AuthzDenied(
            400, "field_not_settable",
            f"`update` ne pose pas : {', '.join(f'`{c}`' for c in inertes)}. Ces "
            "champs se déclarent à la création et ne se retouchent pas — une autre "
            "valeur, c'est une autre automatisation. Les champs modifiables sont : "
            f"{', '.join(f'`{c}`' for c in db.CHAMPS_MODIFIABLES)}.")
    # `tools` reste modifiable, `descriptions_outils` non : une allowlist qui retirerait un
    # outil que le réglage nomme le rendrait inerte sans un mot (oto#241).
    if inp.tools is not None:
        actuelle = db.get_fleet(inp.fleet_id, ctx.org_id)
        # Depuis un agent hébergé, la liste d'une campagne ne s'élargit pas (#1499) :
        # chaque passage relit SA liste (`campagne_a_servir`), donc une campagne armée
        # par une personne s'élargissait au passage suivant. Même règle qu'un agent ;
        # réduire, ou reposer la même liste, passe. `procedure` est figée (`update` ne
        # la pose pas) : la liste servie ne bouge que par `tools`. Lancer ou armer une
        # campagne plus outillée que le travail appelant reste permis (relais) : sa
        # liste, elle, est posée par une personne.
        if actuelle:
            _outils_plateforme.exiger_aucun_ajout_d_agent(actuelle, inp.tools)
        retires = _descriptions_outils.outils_nommes_retires(
            (actuelle or {}).get("descriptions_outils"), inp.tools)
        if retires:
            raise AuthzDenied(
                400, _descriptions_outils.CODE,
                f"`tools` retirerait {', '.join(retires)}, que `descriptions_outils."
                "entieres` nomme : le réglage de cette automatisation cesserait d'agir. "
                "Garde ces outils, ou déclare une autre automatisation.")
    f = db.update_fleet(inp.fleet_id, ctx.org_id, champs)
    if not f:
        raise AuthzDenied(404, "fleet_not_found", "automatisation inconnue")
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
                          "sur une automatisation sans `fleet_id`"),
            DeclaredError(400, "target_incomplete",
                          "`row_filter` sans `namespace` — un périmètre suppose un "
                          "tableau"),
            DeclaredError(400, "target_is_frozen",
                          "`namespace`/`row_filter` après la déclaration : la cible "
                          "d'une automatisation ne se déplace pas"),
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
            DeclaredError(400, "no_runner_armed",
                          "`launch` dans une org où rien n'exécute les automatisations "
                          "pour l'instant : l'armement réussirait sans que rien ne "
                          "s'exécute jamais"),
            DeclaredError(400, "model_required",
                          "`create` sans `model`, ou `launch` d'une automatisation "
                          "déclarée sans modèle — un agent hébergé tourne sur la clé "
                          "de modèle de son org"),
            DeclaredError(400, "model_not_served",
                          "`launch` d'une automatisation dont la famille de modèle "
                          "n'est servie par rien en ce moment"),
            DeclaredError(400, "model_key_required",
                          "`launch` d'une automatisation dans une org qui doit tourner sur SA "
                          "clé de modèle et ne l'a pas déposée"),
            DeclaredError(404, "datastore_not_found",
                          "`create` sur un `namespace` qu'aucun tableau de la portée "
                          "du déclarant ne porte"),
            DeclaredError(409, "datastore_ambigu",
                          "`create` sur un nom que plusieurs tableaux portent au même "
                          "rang de la portée du déclarant, ou `launch` d'une "
                          "automatisation d'avant #1067 dont le nom ne désigne plus un "
                          "seul tableau"),
            DeclaredError(404, "fleet_not_found",
                          "automatisation inconnue dans l'org du porteur"),
            DeclaredError(409, "not_launchable",
                          "`launch` d'une automatisation déjà armée ou en cours"),
            DeclaredError(409, "max_rows_reached",
                          "`launch` d'une automatisation qui a déjà produit ses "
                          "`max_rows` exécutions : armée, elle n'en produirait plus "
                          "aucune"),
            DeclaredError(409, "not_stoppable",
                          "`stop` d'une automatisation ni armée ni en cours"),
            DeclaredError(409, "not_takeable",
                          "`take` d'une automatisation ni `armed` ni `running`"),
            DeclaredError(409, "held_by_other",
                          "`take` d'une automatisation `running` qu'un AUTRE ordonnanceur "
                          "tient : deux ordonnanceurs doubleraient ses exécutions"),
            DeclaredError(409, "not_the_holder",
                          "`beat`/`ack_stop` par un ordonnanceur qui ne tient pas "
                          "(ou plus) l'automatisation"),
            DeclaredError(409, "nothing_to_acknowledge",
                          "`ack_stop` sans arrêt en cours"),
        ),
        rest=RestBinding(verb="POST", path="/api/me/runner/fleets"),
        description=(
            "Declared configuration of an agent PASS — what a fleet runs, on which "
            "table, within which perimeter, and up to which limit. op=create "
            "(`label` + procedure slug + `tools` allowlist ; optional target "
            "`namespace` + `row_filter`, execution context `model` — REQUIRED, one "
            "of the catalogue served as `runner.models` by oto_trigger (refused "
            "`model_required` when omitted: the pass runs on its organization's "
            "model key); `provider` is deduced from it —, and "
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
            "between the two is also the diagnosis. op=launch is REFUSED with "
            "`no_runner_armed` when no worker polls for this org at all (nothing "
            "would ever execute it — reading, updating and op=stop stay open), and "
            "with `model_not_served` when the fleet declares a model no live worker "
            "serves: its jobs would wait forever — and `model_required` for a fleet "
            "declared without a model (declare another one). "
            "op=state returns the pass PROGRESS aggregated "
            "over its jobs — pending, claimed, done, failed, abandoned, tokens "
            "consumed, heaviest single row, `empty_jobs` (finished with no row), "
            "`stopped_after_write` — plus `reservable_rows`, what the scheduler still "
            "sees to serve — and says `no_jobs_attached` "
            "explicitly rather than returning zeros you would read as 'nothing "
            "happened'. ⚠️ Those counters count JOBS, never rows: a job that gives its "
            "row back unwritten still ends `done`, so `done` = `jobs_total` with "
            "`failed` = 0 is NOT a success. What became of the ROWS is in `rows` — per "
            "final status value, `concluded`, `abandoned` (failed, with the reason) and "
            "`open`, read from the table itself — and what each execution concluded is "
            "in `runs_by_outcome` (done / partial / failed / blocked). Report the "
            "outcome from `rows`, and say its `scope`. The TARGET is frozen at declaration: redirecting a running "
            "pass to another table is what declaring exists to prevent; the "
            "execution context (`provider`/`model`) is frozen too, since changing it "
            "mid-flight falsifies the attribution of rows already written — declare "
            "another fleet instead — duplicate, never switch. An EXTERNAL scheduler "
            "is still served here, and each of its calls names it with `taken_by` "
            "(its own id, stable across its restarts): op=take (`armed`→`running`, "
            "records `taken_by` — returned by get and list; taking back a `running` "
            "pass it already holds is a resume and succeeds, one held by ANOTHER "
            "scheduler is refused `held_by_other`), op=beat (heartbeat AND reads back "
            "`stop_requested` in the same call), op=ack_stop (`stopping`→`stopped`) "
            "— both refused `not_the_holder` to anyone but the holder. "
            "⚠️ None of them is required any more, and you should not call them: "
            "polling alone moves a pass, and op=take would only claim one that was "
            "about to start on its own."
        ),
    ),
]
