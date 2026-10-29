"""Capacité « la file d'exécutions du runner » — REST-only, op-aware (chantier R2).

C'est la face que consomme le WORKER externe (`oto-runner`) : enfiler, réclamer,
lier au run ouvert, prolonger le bail, conclure. Pas de face MCP : un agent en
conversation n'a rien à faire dans la plomberie d'exécution — le précédent est la
pose de secrets (dashboard-only) ; ici c'est worker-only, même logique.

**Le scope EST l'org de l'appel** (V1) : un worker porte un jeton d'org et ne voit
que la file de cette org. Le pool multi-org attend l'arbitrage compte-de-service
(ADR 0064 §5-1) — rien ici ne le préjuge, le claim prendra un scope plus large le
jour où l'identité le permettra.

**Un job porte des RÉFÉRENCES, jamais un secret** : la procédure à charger, le
projet, le run à continuer. Le worker résout tout le reste par ses trois contrats
(API de fil, face MCP, clé de modèle) — un payload qui transporterait un credential
serait un coffre parallèle.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import (_abonnement, _acces_agent, _cle_exigee, _limites_du_run,
               _lignes_reservables, _modele, _ordre_de_service)
from .. import db, outils_du_travail, runner_consigne, runner_models, time_saved, tool_alias
from ..db import user_subscriptions
from ..access import credits as budget
from ..access import credits_tours as tours
from ._authz import WORKER_OR_ORG_MEMBER
from ._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                     RestBinding, cap_limit)
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)


class JobsInput(BaseModel):
    op: Literal["enqueue", "claim", "bind_run", "complete", "extend", "get", "list",
                # Ce que la page Agents trace, pour toute l'org en UN appel : voir
                # `_resume`. Lecture de membre, jamais de worker.
                "summary",
                # UN appel au modèle sur une clé PARTAGÉE (18/10/2026) : réservé avant
                # d'être envoyé, réglé à son usage avant que le worker continue —
                # voir `_reserver_un_tour` et `_regler_un_tour`.
                "reserve", "meter",
                # Ce que la SONDE du worker a vu du forfait, AVANT le run (21/10/2026) :
                # le backend le porte et tranche — voir `_trancher_le_forfait`.
                "forfait"]
    # enqueue —
    kind: Optional[Literal["start", "continue"]] = None
    payload: Optional[dict[str, Any]] = None
    run_id: Optional[str] = None
    max_attempts: int = 3
    # enqueue : la flotte à laquelle rattacher le travail. `list` : le filtre qui
    # restreint la page (et son `total`) aux travaux de CE passage — l'historique
    # d'un passage se lit ainsi sans balayer la file de l'org.
    fleet_id: Optional[int] = None
    # list : les travaux enfilés par CE déclencheur (lu dans le payload, où le
    # tick le pose). Servi pour la même raison que `fleet_id`.
    trigger_id: Optional[int] = None
    # claim — le worker nomme le dépôt de clé qu'il sait consommer (voir
    # `_cle_de_modele`). Absent : il tourne sur la clé de la plateforme.
    # ⚠️ C'est aussi la FAMILLE de modèles qu'il sert : il ne réserve que les
    # travaux de cette famille, et ceux qui n'en portent aucune.
    provider: Optional[str] = None
    # claim — le worker ne tient AUCUNE clé de modèle à lui (13/09/2026) : il ne
    # tourne que sur la clé que l'organisation du travail a déposée. Deux effets,
    # tous deux nécessaires : il ne réserve que les travaux de SA famille (jamais
    # ceux d'un agent posé sans modèle, que les workers existants servent), et un
    # travail dont l'org n'a pas déposé cette clé est ARRÊTÉ à la réservation,
    # raison écrite — jamais remis sans clé à un worker qui n'en a pas.
    # ⚠️ Exige `provider` : sans dépôt nommé, il n'y a aucune clé à attendre.
    org_key_only: bool = False
    # claim — ne réserver QUE les travaux de ces orgs (25/09/2026) : essayer un moteur
    # sur une organisation avant de le donner au parc. Absent = toutes, comme avant.
    org_ids: Optional[list[int]] = Field(None, min_length=1, max_length=50, description=(
        "claim: only take jobs of these organizations (trial a worker on a few orgs). "
        "Unset = all."))
    # claim — le MOTEUR d'exécution du worker (29/09/2026). `farm` = Claude Code dans la
    # ferme : seul un tel worker réserve les travaux d'une org routée vers la ferme
    # (option d'org `claude_farm`). Absent = la boucle, comme avant.
    meters_usage: bool = Field(False, description=(
        "claim: this worker reserves EVERY model call before sending it (`op=reserve`) "
        "and reports its usage (`op=meter`) before its next step, and stops when either "
        "is refused. Only such a worker is served a key the instance re-bills (the "
        "platform's, or its primary tenant's), whose calls are settled in the credit "
        "ledger: a worker that does not declare "
        "it never receives one."))
    forfait_sonde: bool = Field(False, description=(
        "claim: this worker probes the Claude plan and reports it with `op=forfait` "
        "before running a job served with `forfait_a_sonder: true`. Such a worker is "
        "also served jobs whose plan pause is older than the verification window and "
        "whose limiting window is unknown: the probe decides. A pause whose window "
        "is known waits for its reset. A worker that does not declare it keeps "
        "waiting for the stored reset."))
    # reserve / meter — le rang du tour dans la tentative (0, 1, 2…).
    turn: Optional[int] = Field(None, ge=0)
    # reserve — le `max_tokens` de l'appel : la seule borne connue avant l'appel.
    max_tokens: Optional[int] = Field(None, ge=1)
    engine: Optional[Literal["farm"]] = Field(None, description=(
        "claim: this worker's execution engine. `farm` = Claude Code in the farm. Jobs "
        "of the `anthropic` family of an organization routed to the farm (org option "
        "`claude_farm`) are reserved ONLY by a `farm` worker: any other worker never "
        "sees them, and without a live `farm` worker they wait. Platform workers only, "
        "with `provider=anthropic`. Unset = the regular loop."))
    # claim / extend —
    lease_seconds: int = 600
    # bind_run / complete / extend / get —
    job_id: Optional[int] = None
    ok: Optional[bool] = None
    error: Optional[str] = None
    # complete — résultat déclaré par le worker (usage_tokens, stopped, steps…),
    # lu par un ordonnanceur de flotte (garde budget, R5). Jamais du contenu de fil.
    result: Optional[dict[str, Any]] = None
    # list — surveillance dashboard : la file de l'org, du plus récent au plus ancien.
    status: Optional[Literal["pending", "claimed", "done", "failed"]] = None
    source: Optional[Literal["batch", "scheduled", "manual"]] = Field(
        None,
        description=(
            "D'OÙ vient le travail, sur `list` : `batch` (un passage de flotte), "
            "`scheduled` (un déclencheur programmé), `manual` (un appel direct). "
            "Servi côté serveur À DESSEIN — la file est paginée et un passage de "
            "2 000 lignes remplit une page à lui seul : trié côté client, "
            "`scheduled` rendrait vide sur une org qui en joue un chaque matin. "
            "`total` compte sous le MÊME filtre."),
    )
    limit: int = 50
    # summary — la fenêtre, en jours (1 à 31). La page en trace au plus 28.
    days: int = Field(28, ge=1, le=31, description=(
        "summary: how many days back to count (1-31)."))
    # list — la page suivante, telle que la réponse précédente l'a rendue. Opaque :
    # sa composition nous appartient (même parti que `data_rows` et les lignes d'un
    # nœud), le worker la renvoie telle quelle.
    cursor: Optional[str] = None

    @field_validator("limit")
    @classmethod
    def _cap_limit(cls, v):
        # Écrêter, pas refuser (patron `cap_limit`, #300) — mais l'écrêtage n'est
        # plus muet : `total` et `next_cursor` disent ce qui reste. La borne est
        # DÉCLARÉE ici, au contrat, et non plus seulement dans le LIMIT du SQL.
        return cap_limit(v, db.JOBS_PAGE_MAX)


class JobResult(BaseModel):
    """Le résultat DÉCLARÉ par le worker à la conclusion (≤ 4 Ko) — un résumé
    d'exécution, jamais du contenu de fil. `stopped` = le motif d'arrêt de la
    boucle (end_turn, max_steps…) ; `tool_counts` = les appels RÉUSSIS par
    outil — c'est là qu'un « tour perdu » (analyser sans écrire) se lit au
    grain job, sans ouvrir le fil.

    Les trois derniers champs sont les POSTES DE GARDE du harnais : ce qu'il a dû
    réparer sur la ligne travaillée. Ils étaient déjà servis — `extra=allow` les
    laissait passer — mais *servi* n'est pas *déclaré* : leur forme n'était garantie
    nulle part et un client typé ne les voyait pas. Ils sont nommés ici pour que
    `valeurs_cliente_detruites` en particulier soit lu comme il doit l'être :
    **`null` n'y est pas une liste vide**.

    `extra=allow` reste : le worker déclare davantage (coût d'entrée/sortie, cache,
    hors-schéma, faux départ, ligne abandonnée…), et le schéma nomme le socle sans
    le fermer."""
    model_config = ConfigDict(extra="allow")

    usage_tokens: Optional[int] = None
    stopped: Optional[str] = None
    steps: Optional[int] = None
    tool_counts: Optional[dict[str, int]] = None
    abonnement: Optional[dict[str, Any]] = Field(
        None, description=(
            "Subscription jobs only (`model_family` ending in `_subscription`): what "
            "the worker SAW of the requester's plan while running. Shape: `{etat, "
            "deconnecte?, fenetres: {<window>: {utilization, resetsAt}}}` — `etat` "
            "and the windows are copied from the provider's `rate_limit_event` "
            "(`rate_limit_info.status`, `unifiedWindows`), `resetsAt` in epoch "
            "seconds. The backend pauses the requester's subscription jobs when a "
            "window reaches its threshold, until that window resets. "
            "`deconnecte: true` = the program found no valid session: the person "
            "must reconnect. Never a credential, never the account's email."))
    valeurs_cliente_reparees: Optional[list[str]] = Field(
        None, description=(
            "Guard post: the client's own values the harness had to PUT BACK on the "
            "row (column names), restored from the value the platform kept in "
            "`<column>.origine`. `[]` = the guard ran and had nothing to repair. A "
            "repaired row still counts as a fault — repairing must not make the "
            "defect vanish from the tally."))
    contacts_fabriques_retires: Optional[list[str]] = Field(
        None, description=(
            "Guard post: fabricated contacts REMOVED from the row (their names), not "
            "merely flagged — a flagged row still gets called. `[]` = the guard ran "
            "and found none."))
    valeurs_cliente_detruites: Optional[list[str]] = Field(
        None, description=(
            "Guard post: the client's own values found destroyed on the row (column "
            "names). ⚠️ THREE states, not two: a list = those columns; `[]` = "
            "measured, none destroyed; **null = NOT MEASURED** — the harness could "
            "not identify the row it worked (the `conversations` path resolves it by "
            "alias and does not always succeed), so the check never ran. Reading null "
            "as \"no destruction\" would report a clean run where nothing was looked "
            "at."))


class Job(BaseModel):
    """Un job tel que servi — Optional là où les PROJECTIONS divergent : le
    claim rend (id, kind, run_id, payload, attempts, max_attempts, lease_until)
    sans status ni result ; list/get rendent le reste, `lease_until` compris."""
    id: int
    kind: Optional[str] = None
    run_id: Optional[str] = None
    fleet_id: Optional[int] = Field(
        None, description=(
            "The FLEET this job belongs to, when it was enqueued for a declared "
            "pass. Null for a standalone job (trigger, direct call). This is what "
            "makes `runner.fleets op=state` able to aggregate a pass — without it "
            "a pass is only readable by correlating timestamps by hand."))
    sub: Optional[str] = Field(
        None, description=(
            "WHOSE identity this job carries — the account the agent acts as while "
            "running it, not an audit trail. Defaults to whoever created the "
            "trigger. Null on jobs enqueued before 2026-09-02: their requester is "
            "unknown, and no default was invented for them — a null that says 'we "
            "do not know' beats a name that would be read as a fact."))
    org_id: Optional[int] = None
    delegated_token: Optional[str] = Field(
        None, description=(
            "A short-lived API token issued IN THE NAME OF this job's `sub`, "
            "returned by op=claim only. The worker is not a privileged actor: it "
            "is an ordinary MCP client carrying the requester's identity. Use it "
            "for every call made while executing this job, then drop it — it "
            "expires with the lease. Absent on jobs with no known requester "
            "(enqueued before 2026-09-02): fall back to your own token."))
    model_key: Optional[str] = Field(
        None, description=(
            "The MODEL PROVIDER KEY this job's organisation deposited, returned by "
            "op=claim only, when the worker named a deposit it can consume. Use it "
            "for this job's model calls instead of your own environment key, then "
            "drop it — it is the org's secret, not yours, and it is never written "
            "to a log or a thread. Absent means the org deposited none: fall back "
            "to the platform key."))
    model_workspace: Optional[str] = Field(
        None, description=(
            "The provider WORKSPACE deposited with `model_key`, returned by op=claim "
            "only, when the org set one. An organisation-level Anthropic key requires "
            "it on every request (header `anthropic-workspace-id`). Not a secret, but "
            "never written to a log either. Absent: send no workspace."))
    delegation_refusee: Optional[str] = Field(
        None, description=(
            "WHY this job cannot run: the account that scheduled it no longer "
            "exists, or no longer holds a role in that organisation — or the "
            "organisation must run its agents on its OWN model key and has not "
            "deposited it (or this worker names no key provider). The job is "
            "already marked failed with this reason — do NOT retry it, and do not "
            "silently drop it either: report the reason. An agent whose identity "
            "is no longer valid stops SAYING SO."))
    payload: Optional[dict[str, Any]] = None
    # Domaine fermé par le CHECK `runner_jobs_status_check` (#437).
    status: Optional[Literal["pending", "held", "claimed", "done", "failed", "expired"]] = None
    attempts: Optional[int] = None
    max_attempts: Optional[int] = None
    claimed_by: Optional[str] = None
    lease_until: Optional[str] = Field(
        None, description=(
            "When the current take's lease expires. Read it AGAINST `status`: on a "
            "`claimed` job, past = the worker is gone and the job is reclaimable — "
            "the fact itself, not a staleness threshold guessed from `created_at`. "
            "On a concluded job it is the lease that WAS held (`done` keeps it; a "
            "re-queued failure clears it), and null on a `pending` job that no one "
            "has taken."))
    last_error: Optional[str] = None
    attempt_errors: Optional[list[dict]] = Field(
        None, description=(
            "The reason of EVERY attempt, oldest first: `[{attempt, at, error}]`. "
            "⚠️ `last_error` is only the LAST one and overwrites the rest — three "
            "attempts that fail differently are not three attempts that fail the "
            "same way, and a job that SUCCEEDS on its third try kept no trace at "
            "all of the two that didn't (the success clears `last_error`), which "
            "is the quietest way an incident disappears. A job stopped for good at "
            "the claim, or expired untaken, writes its reason here too (`attempt: 0` = "
            "never taken). `[]` is a real empty (nothing failed); null means the "
            "column was not read."))
    result: Optional[JobResult] = None
    due_at: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None


class SummaryHour(BaseModel):
    """Combien de travaux d'un statut sont tombés dus dans une heure."""
    hour: str
    status: Literal["pending", "held", "claimed", "done", "failed", "expired"]
    count: int


class SummaryFailure(BaseModel):
    due_at: str
    error: Optional[str] = None


class TimeSavedResult(BaseModel):
    """Un type de résultat d'un agent sur la fenêtre : combien, et ce que ça vaut."""
    kind: Literal["report", "action", "record", "research"]
    label: str
    count: int
    minutes_each: float
    #: Ce que ce type a fait gagner — la recherche plafonnée run par run.
    minutes: float


class TimeSavedNotCounted(BaseModel):
    """Des runs conclus `done` d'un agent que le temps gagné ne compte PAS, et pourquoi
    — dit, jamais remplacé par une estimation."""
    #: no_logged_calls : le run n'a aucun appel au journal (`tool_calls`) — pas de
    #: `run_id` lié, ou rien de journalisé sous lui.
    reason: Literal["no_logged_calls"]
    runs: int


class TriggerSummary(BaseModel):
    """Un agent, résumé sur la fenêtre : ses heures non vides, son total, sa
    dernière panne, et le temps qu'il a fait gagner. Seuls les agents que
    l'appelant voit sont rendus."""
    trigger_id: int
    total: int
    hours: list[SummaryHour]
    last_failure: Optional[SummaryFailure] = None
    #: Minutes gagnées par ses runs conclus `done` liés au journal (`time_saved.py`) :
    #: seul ce que les outils DÉCLARENT compte. `0` est un vrai zéro sur les runs
    #: comptés ; ceux qui ne le sont pas sont dans `time_saved_not_counted`.
    time_saved_minutes: float = 0.0
    time_saved: list[TimeSavedResult] = []
    time_saved_not_counted: list[TimeSavedNotCounted] = []


class JobsOut(BaseModel):
    #: summary — par agent visible, ce que la page Agents trace.
    summary: Optional[list[TriggerSummary]] = None
    #: summary — le début de la fenêtre comptée.
    since: Optional[str] = None
    #: summary — les minutes par type de résultat appliquées : des ESTIMATIONS par
    #: défaut, ou les surcharges de l'org — pour que l'écran dise sur quoi repose le
    #: chiffre.
    time_standards: Optional[dict[str, float]] = None
    # enqueue → id/status/due_at ; claim → job (ou null, file vide) ;
    # list → jobs + total + next_cursor ;
    # complete → ok/status + run_id/rows_released/release ; les autres → ok.
    id: Optional[int] = None
    # Relu de la colonne (`RETURNING status`), fermée par `runner_jobs_status_check`.
    status: Optional[Literal["pending", "held", "claimed", "done", "failed", "expired"]] = None
    due_at: Optional[str] = None
    # `enqueue` le RÉPÈTE : l'appelant sait ainsi que son rattachement a été pris,
    # au lieu de le supposer et de découvrir au bilan qu'un passage est vide.
    fleet_id: Optional[int] = None
    # Idem pour l'identité : rendue à l'enfilage, elle se CONSTATE au lieu de se
    # supposer. C'est ce que l'agent portera, donc ce qu'il faut pouvoir vérifier
    # avant qu'il tourne, pas après.
    sub: Optional[str] = None
    job: Optional[Job] = None
    jobs: Optional[list[Job]] = None
    ok: Optional[bool] = None
    # forfait — le verdict de la sonde : `false` = le travail est DÉJÀ rendu à la file,
    # sans tentative comptée ; le worker ne lance rien et ne conclut rien.
    servable: Optional[bool] = None
    # forfait — l'échéance d'un forfait au plafond ; null quand il attend autre chose
    # (une reconnexion).
    limit_reset_at: Optional[str] = None
    # list (#469) — les deux champs sans lesquels une page pleine est indiscernable
    # d'une file épuisée. Un relevé tronqué SOUS-DÉCLARE : il rassure exactement
    # quand il ne faut pas.
    total: Optional[int] = Field(
        None, description=(
            "list: how many jobs the queue holds under the SAME filters (org + "
            "`status`), regardless of `limit` and of where the cursor is. This is "
            "the number a fleet report wants; `len(jobs)` is only one page of it."))
    next_cursor: Optional[str] = Field(
        None, description=(
            "list: pass it back as `cursor` to read the next (older) page — opaque, "
            "do not parse. null = this page is the end of the queue. A full page "
            "WITH a next_cursor means the reading is truncated here."))
    # complete (#633) — le témoin que la clôture du travail rend à un poste de flotte.
    run_id: Optional[str] = Field(
        None, description=(
            "complete: the run whose datastore leases were released — the call's "
            "`run_id`, else the one bound to the job (bind_run/enqueue). null: no run "
            "known, nothing to release by run."))
    rows_released: Optional[int] = Field(
        None, description=(
            "complete: datastore rows the run still held, now back in the queue — "
            "0 is written explicitly (the run held nothing). null: no release was "
            "done, `release` says why."))
    release: Optional[Literal["ok", "no_run", "failed"]] = Field(
        None, description=(
            "complete: outcome of the release step — ok (count in rows_released), "
            "no_run (no run known to this job), failed (the release itself errored; "
            "the job is concluded anyway, the leases expire on their own)."))
    # claim — la DIFFÉRENCE entre « rien à faire » et « je n'ai pas pu regarder ».
    campaign_error: Optional[str] = Field(
        None, description=(
            "claim: set ONLY when `job` is null AND producing work from a running "
            "campaign FAILED. Absent means the queue is genuinely empty. Without "
            "this field the two are indistinguishable, and a campaign that cannot "
            "produce reads as a campaign with nothing left to do — measured on "
            "2026-09-07, a broken query made every poll answer 'no work' for days "
            "while three workers polled and nothing ever ran. Log it: it names a "
            "platform fault, never something the worker can fix."))


def _curseur(dernier_id: int) -> str:
    """Le curseur servi : l'id du dernier job de la page, encodé. Opaque par
    CONTRAT — pas par secret : ce qu'il encode peut changer (un tri neuf changerait
    sa clé), et un worker qui l'aurait décodé casserait ce jour-là."""
    return base64.urlsafe_b64encode(f"job:{dernier_id}".encode()).decode()


def _depuis_curseur(cursor: str) -> int:
    """L'inverse — et un REFUS NOMMÉ si le curseur est abîmé. Repartir du début en
    silence reservirait la première page en boucle : une marche qui ne progresse pas
    et personne pour le voir."""
    try:
        brut = base64.urlsafe_b64decode(cursor.encode()).decode()
        prefixe, _, valeur = brut.partition(":")
        if prefixe != "job":
            raise ValueError(prefixe)
        return int(valeur)
    except Exception:  # noqa: BLE001
        raise AuthzDenied(
            400, "invalid_cursor",
            "`cursor` illisible — reprends celui que la réponse précédente a rendu "
            "dans `next_cursor`, ou omets-le pour repartir du début de la file."
        ) from None


def _release_run_rows(run_id: Optional[str], erreur: Optional[str] = None) -> dict:
    """Le job conclu ne travaille plus : ce que son run tenait encore dans le
    datastore revient dans la file (#633) — la même troisième voie que `run_finish`,
    pour l'agent qui est MORT sans l'appeler (le worker, lui, survit à l'agent et
    conclut le job). Best-effort et HORS de la clôture : le job est déjà conclu
    quand on arrive ici, et un poste de flotte lit le compte — `0` écrit, ou `null`
    avec sa raison, jamais un 0 fabriqué.

    `erreur` = l'échec que le worker vient de déclarer : une ligne que ce relâchement
    fait abandonner le porte dans son motif (#491)."""
    if not run_id:
        return {"run_id": None, "rows_released": None, "release": "no_run"}
    try:
        n = db.datastore_release_by_run(run_id, erreur=erreur)
    except Exception:  # noqa: BLE001
        logger.warning("libération des lignes du run %s à la conclusion du job "
                       "échouée (best-effort)", run_id, exc_info=True)
        return {"run_id": run_id, "rows_released": None, "release": "failed"}
    return {"run_id": run_id, "rows_released": int(n), "release": "ok"}


def _identite_invalide(sub_porteur: str, org_id: int) -> Optional[str]:
    """Pourquoi ce porteur ne peut plus agir — ou None s'il le peut encore.

    Les trois cas arrêtés le 02/09 : compte supprimé, sortie de l'organisation,
    rôle retiré. ⚠️ **Les deux derniers ne se distinguent pas dans le modèle
    actuel** — être membre, c'est avoir un rôle : `org_members` porte les deux en
    une ligne. La raison rendue le dit donc en une phrase plutôt que d'inventer
    une distinction que la base ne fait pas.
    """
    from .. import org_store

    if db.get_user(sub_porteur) is None:
        return ("le compte qui a programmé ce travail n'existe plus")
    if org_store.get_org_role(org_id, sub_porteur) is None:
        return ("le compte qui a programmé ce travail n'a plus de rôle dans cette "
                "organisation (parti, ou droit retiré)")
    return None


def _engine_ferme(ctx: ResolvedCtx, inp: "JobsInput") -> bool:
    """Le worker se déclare-t-il exécutant de la FERME (`engine=farm`) ?

    ⚠️ Refusé plutôt que deviné : se dire de la ferme ouvre les travaux qu'une org a
    réservés à la ferme (`db.OPTION_FERME`). Seul un worker de PLATEFORME (secret de
    machine) le peut, et seulement pour la famille que la ferme sert — un worker au
    jeton d'org, ou un `provider` d'une autre famille, qui s'en réclamerait
    prendrait des travaux qu'il n'exécutera pas dans la ferme."""
    if inp.engine != "farm":
        return False
    if not ctx.platform_worker:
        raise AuthzDenied(
            403, "farm_engine_platform_only",
            "`engine=farm` est réservé aux workers de plateforme de la ferme : un "
            "porteur au jeton d'org ne réserve pas les travaux routés vers la ferme.")
    if inp.provider != db.FAMILLE_FERME:
        raise AuthzDenied(
            400, "farm_engine_family",
            f"`engine=farm` exige `provider={db.FAMILLE_FERME}` : la ferme ne sert que "
            f"cette famille (reçu : {inp.provider!r}).")
    return True


def _cle_de_modele(org_id: int, depot: str) -> tuple[Optional[str], Optional[str]]:
    """La clé de modèle DÉPOSÉE PAR L'ORG et son workspace : `(clé, workspace)`, ou
    `(None, None)` si elle n'en a pas posé.

    ⚠️ Le workspace (14/09/2026) : une clé d'ORGANISATION Anthropic fait refuser toute
    requête qui ne nomme pas le workspace à facturer (en-tête `anthropic-workspace-id`).
    Il vit dans `meta` de la MÊME ligne (champ déclaré `in_meta`) et se lit par la MÊME
    lecture que la clé — aucun chemin de plus vers le secret. Seul un champ DÉCLARÉ sort
    (`meta_fields`) : les satellites de service de `meta` ne partent pas au worker.

    ⚠️ La garde tient au TYPE, pas au nom du connecteur. Un worker qui pourrait
    nommer n'importe quel dépôt tirerait le secret Folk ou Salesforce de l'org au
    moment de réserver un travail : seuls les connecteurs `kind="credential"` —
    ceux dont porter une clé est la SEULE raison d'être, sans aucun outil derrière
    — passent ici. Le type distinct n'est pas un détail d'écran : c'est la liste
    d'autorisation, et c'est pourquoi il fallait un type plutôt qu'un connecteur
    ordinaire aux namespaces vides.

    ⚠️ Et la clé remise est celle de l'ORG DU TRAVAIL, jamais celle du worker ni
    celle d'une org qu'il nommerait : le worker ne choisit que le DÉPÔT, jamais
    à qui il appartient.
    """
    from .. import credentials_store, providers
    c = providers.connector_for_provider(depot)
    if not c or c.kind != "credential":
        return None, None
    try:
        ligne = credentials_store.get_credential_with_meta("org", str(org_id), depot)
    except Exception:
        # Un coffre qui ne rend pas la clé n'empêche pas le travail : le worker
        # retombe sur la clé de la plateforme. Mais le silence, lui, est refusé —
        # une org qui a déposé sa clé et qu'on facture sur la nôtre doit se voir.
        logger.warning("clé de modèle `%s` illisible pour l'org %s",
                       depot, org_id, exc_info=True)
        return None, None
    if not ligne or not ligne.get("secret"):
        return None, None
    workspace = credentials_store.meta_fields(depot, ligne.get("meta") or {}).get("workspace_id")
    return ligne["secret"], (workspace or None)


# La marque qui dit « ce compte EST un de nos workers ». Un admin plateforme la
# pose sur le compte de service du runner (`oto_admin_set_option`), et sur lui
# seul.


def _depot_pose(org_id: int, depot: str) -> Optional[bool]:
    """Cette org a-t-elle DÉPOSÉ cette clé — présence seule, sans déchiffrer.

    Sert à décider s'il y a quelque chose à refuser, donc quelque chose à dire.
    `has_credential` lit la présence de la ligne du coffre, sans la déchiffrer : le
    secret n'est jamais touché pour écrire une ligne de journal.

    `account=""` — le mono-compte, exactement ce que la remise lit : signaler un
    refus sur un dépôt qu'on n'aurait de toute façon pas servi serait un faux.

    `None` = le coffre n'a pas répondu (18/10/2026) : la réservation ne sert alors
    rien et n'arrête rien (`_doute_du_coffre`) — servir pourrait faire payer notre
    clé sans mesure, arrêter écrirait une raison fausse.
    """
    from .. import credentials_store, providers
    c = providers.connector_for_provider(depot)
    if not c or c.kind != "credential":
        return False
    try:
        return credentials_store.has_credential("org", str(org_id), depot, account="")
    except Exception:
        logger.warning("présence du dépôt `%s` illisible pour l'org %s",
                       depot, org_id, exc_info=True)
        return None


def _charge_servie(job: dict) -> dict:
    """Le travail SERVI : sa charge, complétée du contexte d'exécution que l'agent
    ne peut pas déduire. Une COPIE — le travail stocké ne change jamais.

    **L'org du travail** (`payload.org_id` = `job.org_id`). Le worker l'impose en
    `_org` à chaque appel qui déclare l'axe ; sans elle, chaque appel se résout
    dans l'org ACTIVE du porteur, et `run_start` y ouvre le run. Les campagnes la
    posaient, les déclencheurs et l'appel direct non. Constaté le 13/09/2026 sur
    un porteur de deux orgs : l'agent d'un déclencheur de l'org B lisait la
    procédure homonyme de son org active A — et écrivait donc là aussi. ⚠️ Une
    valeur contradictoire de la charge est REMPLACÉE, et le dit : la charge est
    écrite par un appelant, l'org du travail est ce que la file a gardé, et c'est
    elle que la délégation vérifie (`_identite_invalide`).

    **L'outil de lecture de la procédure.** La plateforme compose « lis la
    procédure X » (`_instruction`), et l'agent la lit par `oto_procedure`. Le
    worker sert EXACTEMENT `payload.tools` (fail-closed) et ignore ce qu'est une
    procédure ; or une liste DÉDUITE ne cite que les `<tool:…>` de la procédure.
    Sans cet ajout, l'agent ne peut pas lire sa consigne et conclut sans elle —
    vécu du 04 au 06/09/2026, puis masqué par l'injection du texte dans le cadre
    (`system`, v1.244.0), retirée le 13/09/2026.

    ⚠️ Rien de tout cela n'est un droit : l'org servie est celle où le porteur a
    été vérifié, et un appel hors de ses droits reste un refus nommé, jamais un
    repli sur son org active. Une liste d'outils qui n'est pas une liste n'est pas
    réparée ici."""
    p = dict(job.get("payload") or {})
    org_id = job["org_id"]
    if "org_id" in p and p["org_id"] != org_id:
        logger.warning("travail %s : `payload.org_id` %r contredit l'org du travail %s — "
                       "servi avec celle du travail", job.get("id"), p["org_id"], org_id)
    p["org_id"] = org_id
    # La MÊME liste que celle que le serveur tient à chaque appel (`outils_du_travail`).
    if "tools" in p or p.get("procedure"):
        p["tools"] = outils_du_travail.outils_servis(p)
    return {**job, "payload": p}


def _avec_cle(job: dict, depot: Optional[str], appelant: str, *,
              worker: bool, org_key_only: bool = False, metre: bool = False,
              ferme: bool = False) -> Optional[dict]:
    """Le travail, augmenté de la clé de modèle de son org — à la RÉSERVATION.

    Le worker fait partie du backend et a le droit de lire les clés que les orgs
    déposent (arbitrage du 02/09) ; ce droit s'exerce ici, une fois, avec le
    travail — jamais par un accès au coffre depuis le runner. Un worker qui
    saurait interroger le coffre pourrait lire autre chose que ce travail-ci.

    ⚠️ **Mais la file n'est pas réservée aux workers** : un membre d'org peut
    enfiler un travail puis le réserver. Sans la garde ci-dessous, il recevrait
    la clé de son org EN CLAIR — un secret que le coffre ne rend à personne, et
    que nous ne pouvons pas révoquer puisqu'il appartient au client.

    `worker` est ce que la règle d'autorisation a établi : le principal s'est
    authentifié par un secret de machine déclaré en base. Ce n'était pas le cas
    avant le 09/09/2026 — la garde lisait une MARQUE posée sur un compte, et
    le compte marqué était un compte personnel.
    """
    if not job.get("org_id") or job.get("delegation_refusee"):
        return job
    if not worker:
        # Silencieux POUR L'APPELANT — il reçoit son travail, sans clé : un refus
        # explicite apprendrait qu'il y a une clé à obtenir.
        #
        # ⚠️ Et silencieux pour NOUS AUSSI tant qu'il n'y a RIEN À REFUSER. Ce
        # journal n'a de sens que si l'org a effectivement déposé une clé :
        # sinon le travail serait parti sans clé de toute façon, et la ligne ne
        # décrit aucun événement. Sans ce filtre, les workers eux-mêmes — qui
        # nomment leur dépôt à CHAQUE réservation, toutes les 15 s, à trois —
        # écrivaient ~17 000 lignes par jour tant que la marque n'était pas
        # posée. Un journal qu'on cesse de lire ne protège plus rien, et c'est
        # la sonde qui aurait fabriqué son propre signal.
        if depot and _depot_pose(job["org_id"], depot):
            logger.warning("clé de modèle `%s` REFUSÉE à %s (org %s, travail %s) : "
                           "ce n'est pas un worker de plateforme",
                           depot, appelant, job["org_id"], job.get("id"))
        return job
    famille = _abonnement.famille_du_travail(job)
    if not famille:
        # ⚠️ UN TRAVAIL SANS MODÈLE NE PART PLUS (24/09/2026). Servi à un worker de
        # plateforme, il tournait sur le modèle de SON environnement — notre clé —
        # et aucune garde d'argent ne mordait : `_cle_exigee` ne juge que la famille
        # déclarée. La pose le refuse désormais (`_modele.exige_un_modele`) ; ceci
        # arrête ce qui a été posé avant, ou enfilé à la main, raison écrite.
        return _refuser_sans_cle(job, appelant, _SANS_MODELE)
    if _abonnement.est_abonnement(famille):
        # ⚠️ **Aucune clé n'est cherchée ici, et c'est le fond du sujet** (OTO-130) :
        # ce travail tournera dans le sandbox de SON DEMANDEUR, sur le programme
        # officiel du fournisseur, avec la session qu'il y a ouverte lui-même. La
        # plateforme ne paie rien, ne détient rien, ne relaie rien. Laisser la garde
        # d'argent d'en dessous s'exécuter le ferait refuser `_SANS_CLE_DEPOSEE` —
        # une clé que personne ne déposera jamais pour cette famille.
        #
        # ⚠️ APRÈS la garde `worker` ci-dessus, pas avant (revue du 21/09/2026) : la
        # file n'est pas réservée aux workers. Placée plus haut, cette branche
        # laissait un simple membre ARRÊTER DÉFINITIVEMENT le travail d'un collègue
        # non connecté, et lui rendait le sandbox d'un autre.
        return _avec_abonnement(job, famille, appelant)
    # ⚠️ LA GARDE D'ARGENT (`_cle_exigee`), et seulement pour un WORKER : c'est lui
    # qui retombe sur la clé de SON environnement — la nôtre — quand le travail
    # n'en porte pas. Un membre qui réserve tourne sur ce qu'il a, pas sur nous.
    if not depot:
        # Un worker qui ne nomme AUCUN dépôt ne consommera jamais la clé de l'org,
        # quoi qu'elle ait déposé : il tournera sur la sienne. Si l'org en exige
        # une, ce worker ne peut PAS la servir — déposée ou non n'y change rien.
        exigees = [f for f in _cle_exigee.fournisseurs_de_modele()
                   if _cle_exigee.cle_exigee(job["org_id"], f)]
        if exigees:
            return _refuser_sans_cle(job, appelant, _SANS_DEPOT)
        return job
    cle, workspace = _cle_de_modele(job["org_id"], depot)
    if not cle and (job.get("payload") or {}).get("_plateforme", {}).get("repli"):
        # ⚠️ UN TRAVAIL REPLIÉ NE SE SERT JAMAIS SUR NOTRE CLÉ (OTO-130). Il a été
        # déplacé d'un abonnement gratuit vers une clé d'org PRÉCISÉMENT parce que
        # cette clé était lisible ; si elle ne l'est plus à la remise, les deux
        # issues d'en dessous sont fausses pour lui : le servir sans clé le ferait
        # payer par la PLATEFORME (le cas `return job`), et l'arrêter
        # définitivement le tuerait alors qu'il avait de quoi attendre.
        #
        # La troisième issue est la bonne, et elle n'existait pas : défaire le
        # repli et le rendre à la file, tel qu'il était. Il redevient un travail
        # d'abonnement qui attend sa réinitialisation — ce qu'il aurait fait si on
        # ne l'avait jamais touché.
        if db.defaire_le_repli(job["id"], appelant,
                               f"clé `{depot}` de l'org illisible à la remise"):
            logger.warning("repli DÉFAIT pour le travail %s (org %s) : la clé `%s` "
                           "n'était plus lisible à la remise — rendu à la file sur "
                           "son abonnement", job.get("id"), job.get("org_id"), depot)
            return None
        # Le défaire a échoué (course sur la prise) : on ne sert RIEN plutôt que de
        # servir sur notre clé. Le bail expirera et le travail repartira.
        logger.warning("repli du travail %s NON défait (course) — rien n'est servi",
                       job.get("id"))
        return None
    payeur = None
    depose = _depot_pose(job["org_id"], depot) if not cle else True
    if depose is None:
        return _doute_du_coffre(job, appelant, depot)
    if not cle and not depose:
        # LA CLÉ PARTAGÉE (18/10/2026) : l'org n'a rien déposé, mais une clé de
        # plateforme (ou de son tenant) lui est ACCORDÉE. Elle paie, et chaque appel
        # au modèle se réserve puis se règle au budget d'unités (`_reserver_un_tour`).
        # ⚠️ Jamais quand l'org a DÉPOSÉ une clé que le coffre n'a pas rendue :
        # elle paie déjà son fournisseur, et la facturer sur la nôtre pour un
        # hoquet du coffre serait la faire payer deux fois. Ce cas garde le
        # comportement d'avant.
        try:
            partagee = _cle_exigee.cle_partagee(job["org_id"], depot, sub=job.get("sub"),
                                                strict=True)
        except _cle_exigee.CleIllisible:
            return _doute_du_coffre(job, appelant, depot)
        posee = None if partagee is not None else _cle_exigee.cle_de_plateforme_posee(depot)
        if partagee is None and posee is None:
            return _doute_du_coffre(job, appelant, depot)
        if partagee is not None:
            # Refacturée : celle de la plateforme ou du tenant PRIMAIRE, que l'opérateur
            # paie (`budget.rang_refacture`). Celle d'un tenant non primaire, ce tenant
            # la paie comme une org la sienne : servie sans mesure, jamais réservée.
            payeur = budget.rang_refacture(partagee.mode, partagee.entity_id)
            if payeur is not None and ferme:
                # La ferme fait tourner Claude Code, dont nous ne mesurons pas chaque
                # appel : elle ne reçoit jamais une clé que nous refacturons.
                return _refuser_sans_cle(job, appelant, _cle_exigee.RAISON_FERME)
            if payeur is not None and not metre:
                # Un worker qui ne mesure pas chaque appel ne reçoit pas notre clé —
                # mais l'org y a droit : le travail retourne à la file, sans compter
                # la tentative, pour un worker qui mesure.
                db.rendre_a_la_file(job["id"], appelant, _ATTEND_UN_WORKER_QUI_MESURE)
                logger.warning("travail %s (org %s) : clé partagée `%s` NON servie à %s, "
                               "qui ne mesure pas chaque appel — rendu à la file",
                               job.get("id"), job["org_id"], depot, appelant)
                return None
            if payeur is not None:
                # LE PLANCHER (#1376, B) : l'entrée du premier tour ne se réserve pas ;
                # un travail que le solde ne couvre pas ne part pas pour mourir au
                # premier appel. AVANT l'estampille : refusé, il n'a rien consommé.
                manque = tours.sous_le_plancher(job.get("sub"), job["org_id"],
                                                (job.get("payload") or {}).get("model"))
                if manque is not None and manque["code"] == budget.INDISPONIBLE:
                    # Un solde ILLISIBLE n'est pas un solde insuffisant : rejouable,
                    # comme au tour (`_REFUS_DU_TOUR`). Ni servi — le plancher n'est pas
                    # su —, ni arrêté — une raison écrite sur un doute serait fausse :
                    # rendu à la file sans compter la tentative.
                    db.rendre_a_la_file(job["id"], appelant, _SOLDE_ILLISIBLE)
                    logger.warning("travail %s (org %s) : solde de crédits illisible au "
                                   "plancher — rendu à la file, ni servi ni arrêté",
                                   job.get("id"), job["org_id"])
                    return None
                if manque is not None:
                    return _refuser_sans_cle(job, appelant, _sous_le_plancher(job))
            if payeur is not None and not db.estampiller_cle_partagee(
                    job["id"], appelant, depot, payeur):
                # Plus à ce worker (course) : rien n'est servi sur une clé qu'on ne
                # saurait pas refacturer. Le bail expirera.
                logger.warning("travail %s : estampille de la clé partagée `%s` NON "
                               "écrite — rien n'est servi", job.get("id"), depot)
                return None
            cle = partagee.key
            # Le workspace d'une clé d'ORGANISATION Anthropic, s'il voyage avec la
            # config résolue. ⚠️ Au rang plateforme, la `meta` n'en fait pas partie :
            # la clé partagée se pose donc en clé de WORKSPACE, qui n'en a pas besoin.
            try:
                workspace = (partagee.config or {}).get("workspace_id") or None
            except Exception:
                logger.warning("travail %s : config de la clé partagée `%s` illisible — "
                               "servie sans workspace", job.get("id"), depot, exc_info=True)
                workspace = None
        elif posee:
            # ⚠️ L'instance PAIE ce fournisseur par une clé partagée, et cette org n'y
            # a pas accès (jamais accordée, ou retirée). Servi sans
            # clé, le travail tournerait sur la clé de l'ENVIRONNEMENT du worker :
            # payée par nous, mesurée nulle part. Arrêté, la raison dit les deux issues.
            return _refuser_sans_cle(job, appelant, _cle_exigee.raison_sans_acces(depot))
    if not cle and depose and (org_key_only or _cle_exigee.cle_exigee(job["org_id"], depot)):
        # ⚠️ DÉPOSÉE MAIS ILLISIBLE (21/10/2026) : la ligne du coffre existe, et sa
        # lecture n'a rien rendu. Les deux refus d'en dessous diraient « elle n'en a
        # pas déposé » — faux, et c'est l'org qu'on enverrait chercher une clé qu'elle
        # a. Un doute sur le coffre : ni servi, ni arrêté, raison écrite.
        return _doute_du_coffre(job, appelant, depot,
                                _CLE_DEPOSEE_ILLISIBLE.format(depot=depot))
    if not cle and org_key_only:
        # ⚠️ Un worker SANS clé de plateforme : le remettre sans clé, c'est un
        # travail qui échouera chez le fournisseur — ou, pire, qui trouvera une
        # clé oubliée dans l'environnement du worker et la fera payer. Arrêté
        # ici, raison écrite, que le réglage `runner.org_key_required` soit posé
        # ou non : ce n'est pas une politique de l'org, c'est ce que le worker
        # est capable de faire.
        return _refuser_sans_cle(job, appelant, _SANS_CLE_DEPOSEE.format(depot=depot))
    if not cle:
        # ⚠️ Lu APRÈS la lecture du coffre, et non sur la seule présence du dépôt :
        # un coffre qui ne rend pas la clé (`_cle_de_modele` rend None et le
        # journalise) laisserait sinon partir un travail « avec clé » qui tournerait
        # sur la nôtre. La lecture effective est la seule vérité ici.
        if _cle_exigee.cle_exigee(job["org_id"], depot):
            return _refuser_sans_cle(job, appelant,
                                     _cle_exigee.raison_du_refus([depot]))
        return job
    # Trace de REMISE : qui, quelle org, quel dépôt, quel travail — jamais la clé.
    # Sans elle, une remise anormale ne laisse aucune trace : le seul endroit où
    # elle se verrait serait la facture de l'org.
    logger.info("clé de modèle `%s` remise à %s pour l'org %s (travail %s, %s)",
                depot, appelant, job["org_id"], job.get("id"),
                f"clé partagée, rang {payeur}" if payeur else "clé non refacturée")
    # Le workspace part À CÔTÉ de la clé, s'il est posé — et reste hors de la trace
    # ci-dessus comme elle.
    return {**job, "model_key": cle, **({"model_workspace": workspace} if workspace else {}),
            # Le worker rapporte alors CHAQUE appel au modèle (`op=meter`) avant de
            # continuer : c'est ce que ce drapeau lui demande.
            **({"meter_usage": True} if payeur else {})}


def _postes_du_resultat(r: dict) -> dict:
    """Les quatre postes d'un usage rapporté par le worker, sous les noms des unités
    du budget (`credits_tours.UNITES_DU_MODELE`)."""
    return {"input_tokens": r.get("usage_input"), "output_tokens": r.get("usage_output"),
            "cache_creation_input_tokens": r.get("usage_cache_write"),
            "cache_read_input_tokens": r.get("usage_cache_read")}


def _tentative_partagee(ctx: ResolvedCtx, job_id: int) -> Optional[dict]:
    """La tentative en cours de ce worker, si une clé PARTAGÉE la paie ; None si la clé
    de l'org la paie. 404 si le travail n'est plus à lui."""
    prise = db.tentative_en_cours(job_id, ctx.sub)
    if prise is None:
        raise AuthzDenied(404, "job_not_found", "job inconnu")
    est = prise.get("cle_partagee")
    if not isinstance(est, dict) or est.get("tentative") != prise["attempts"]:
        return None
    return {**prise, "cle_partagee": est}


_REFUS_DU_TOUR = {budget.INSUFFISANT: 402, budget.SANS_BAREME: 422, budget.INDISPONIBLE: 503}


def _reserver_un_tour(ctx: ResolvedCtx, job_id: int, tour: int, max_tokens: int) -> dict:
    """UN appel au modèle, payé par une clé PARTAGÉE, réservé AVANT d'être envoyé : son
    `max_tokens` de sortie, au barème du modèle que nomme sa charge
    (`access/credits_tours.reserver_tour` : le worker n'a pas à le redire). Le worker
    s'ARRÊTE sur un refus : un travail n'appelle pas le modèle sur notre clé sans que
    le budget le couvre.

    - Refusé en `enforce` seulement : `insufficient_credits` (402), `credits_unpriced`
      (422), `spend_ledger_unavailable` (503, rejouable). `report` réserve quand même.
    - **Idempotent** : le même tour redemandé rend la même réponse.
    - Un travail servi par la clé de SON org n'a rien à réserver : `held: false`."""
    prise = _tentative_partagee(ctx, job_id)
    if prise is None:
        return {"ok": True, "held": False}
    try:
        r = tours.reserver_tour(tours.cle_du_tour(job_id, prise["attempts"], tour), prise["sub"],
                                 prise["org_id"], max_tokens, prise.get("model"))
    except tours.TourRefuse as e:
        raise AuthzDenied(_REFUS_DU_TOUR[e.code], e.code, str(e)) from None
    return {"ok": True, **r}


def _regler_un_tour(ctx: ResolvedCtx, job_id: int, tour: int, usage: dict) -> dict:
    """L'usage d'UN appel au modèle, payé par une clé PARTAGÉE, réglé au budget d'unités
    — AVANT que le worker continue (18/10/2026) : ses quatre comptes de jetons, chacun
    sous sa ligne du barème (`access/credits_tours.regler_tour`). L'entrée et le cache, qui
    ne se réservent pas, se règlent au-delà de la réservation (`exceeded`).

    - **Refusé** (422) quand l'entrée ou la sortie n'est pas déclarée : rien ne se
      règle, et le worker s'arrête. Le cache non déclaré se lit zéro, et le règlement
      le dit (`cache_non_declare`).
    - **Idempotent** : un tour déjà réglé (le worker rejoue un rapport dont il n'a pas
      reçu la réponse) ne se règle pas deux fois — `duplicate: true`.
    - Un registre qu'on ne peut écrire refuse en `enforce` (503, le worker s'arrête) ;
      `report` le journalise et rend `metered: false`.
    - Un travail servi par la clé de SON org n'a rien à régler : `metered: false`."""
    postes = _postes_du_resultat(usage)
    if postes["input_tokens"] is None or postes["output_tokens"] is None:
        raise AuthzDenied(422, "usage_unmeasured",
                          "l'entrée et la sortie du tour doivent être déclarées : sans "
                          "elles, l'appel ne se règle pas et le travail s'arrête.")
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
           for v in postes.values() if v is not None):
        raise AuthzDenied(422, "usage_unmeasured",
                          "un compte de jetons est un entier positif ou nul.")
    cache_non_declare = any(v is None for v in postes.values())
    prise = _tentative_partagee(ctx, job_id)
    if prise is None:
        return {"ok": True, "metered": False}
    est = prise["cle_partagee"]
    try:
        r = tours.regler_tour(
            tours.cle_du_tour(job_id, prise["attempts"], tour), prise["sub"], prise["org_id"],
            {k: (v or 0) for k, v in postes.items()},
            {"runner_job_id": int(job_id), "attempt": prise["attempts"], "turn": int(tour),
             "model": usage.get("model"), "key_mode": est.get("mode"),
             **({"cache_non_declare": True} if cache_non_declare else {})},
            prise.get("model"))
    except Exception:
        enforce = tours.mode_du_modele(prise.get("model")) == "enforce"
        logger.exception("travail %s : tour %s NON réglé (clé partagée `%s`) — %s", job_id,
                         tour, est.get("depot"), "refusé" if enforce else "le worker continue")
        if enforce:
            raise AuthzDenied(503, budget.INDISPONIBLE, "The credit ledger could not be "
                              "written: stop, the call is settled when it is reported "
                              "again.") from None
        return {"ok": True, "metered": False}
    if r is None:
        return {"ok": True, "metered": False}
    return {"ok": True, "metered": True, "amount": r["amount"], "exceeded": r["exceeded"],
            "duplicate": not r["settled"]}


def _clore_la_tentative(job_id: int, conclu: dict) -> None:
    """À la conclusion d'une tentative payée par une clé PARTAGÉE : les tours réservés
    et jamais rapportés (l'appel qui a coupé la boucle, un rapport perdu) se règlent
    au montant réservé (`credits_tours.clore_les_tours`). Une tentative qui échoue a
    consommé. Ne lève jamais : la conclusion tient quoi qu'il arrive."""
    estampille = (conclu or {}).get("cle_partagee")
    if not isinstance(estampille, dict) or estampille.get("tentative") != conclu.get("attempts"):
        return
    tours.clore_les_tours(tours.cle_du_tour(job_id, conclu["attempts"], ""),
                          estampille.get("mode"))


def _reprise_future(ligne: dict) -> Optional[datetime]:
    """L'échéance d'une pause EN VIGUEUR (`paused_limit` à échéance future), sinon None.

    ⚠️ Lue en base, l'échéance est un TEXTE « AAAA-MM-JJ HH:MM:SS » (la fabrique de
    lignes du pool), pas un `datetime` : la comparer telle quelle lève."""
    if ligne.get("statut") != user_subscriptions.PLAFOND:
        return None
    reprise = _abonnement.echeance_utc(ligne.get("limit_reset_at"))
    return reprise if reprise is not None and reprise > datetime.now(timezone.utc) else None


def _trancher_le_forfait(ctx, inp: JobsInput) -> dict:
    """`op=forfait` : ce que la SONDE du worker a vu du forfait, AVANT le run.

    Porté sur la connexion qui PAIE, exactement comme le rapport d'une conclusion
    (`noter_rapport` : même seuil, même pause), mais un rapport illisible est REFUSÉ
    (`forfait_report_unreadable`) au lieu d'être avalé. Puis le verdict, relu sur la
    ligne : servable seulement `connected`, ou en pause ÉCHUE (ou sans échéance, ce que
    la réservation sert aussi). Tout autre état — au plafond, à reconnecter, déconnecté,
    sans ligne — RENDS le travail à la file sans tentative comptée : délai de
    revérification en mode personnel, délai court en pool (un autre prêteur le servira).

    ⚠️ Une pause PROPRE À UN MODÈLE (`seven_day_opus`…) n'est pas levée par la sonde :
    oto-runner sonde sur son plus petit modèle, qui ne dit rien de la fenêtre d'un autre.
    Le rapport n'est alors pas porté, et la pause attend son échéance.

    ⚠️ Worker de PLATEFORME seulement, et sur un travail qu'il TIENT : même garde que
    le rapport de `complete` — un membre ne met pas un collègue en pause."""
    if not ctx.platform_worker:
        raise AuthzDenied(403, "platform_worker_only",
                          "`op=forfait` est réservé au worker de plateforme")
    _abonnement.exiger_rapport_lisible(inp.result)
    if inp.job_id is None:
        raise AuthzDenied(400, "missing_fields", "`op=forfait` exige `job_id`")
    conclu = db.porteur_et_famille(inp.job_id) or {}
    if not conclu or not db.tenu_par(inp.job_id, ctx.sub):
        raise AuthzDenied(404, "job_not_found", "job inconnu")
    famille = conclu.get("model_family")
    porteur = conclu.get("abonnement") or conclu.get("sub")
    ligne = (user_subscriptions.get_subscription(porteur, famille) or {}) if porteur else {}
    if (_reprise_future(ligne) is not None
            and user_subscriptions.propre_a_un_modele(ligne.get("limit_fenetre"))):
        logger.info("sonde du forfait (travail %s) non portée : la pause attend la fenêtre "
                    "`%s`, propre à un modèle que la sonde ne mesure pas",
                    inp.job_id, ligne.get("limit_fenetre"))
    else:
        _abonnement.noter_rapport(conclu, True, inp.result)
        ligne = ((user_subscriptions.get_subscription(porteur, famille) or {})
                 if porteur else {})
    statut = ligne.get("statut")
    reprise = _reprise_future(ligne)
    if ligne.get("sandbox_id") and (statut == user_subscriptions.CONNECTE
                                    or (statut == user_subscriptions.PLAFOND
                                        and reprise is None)):
        return {"ok": True, "servable": True}
    pool = db.voie_prise(conclu.get("pool"), inp.job_id)
    db.rendre_a_la_file(inp.job_id, ctx.sub,
                        _abonnement.raison_de_l_attente(famille, statut, pool=pool,
                                                        reprise=reprise),
                        delai_s=(user_subscriptions.RENDU_EN_POOL_S if pool
                                 else user_subscriptions.VERIFICATION_S))
    return {"ok": True, "servable": False,
            "limit_reset_at": reprise.isoformat() if reprise else None}


def _avec_abonnement(job: dict, famille: str, appelant: str) -> dict:
    """Le travail d'un abonnement, servi avec le SANDBOX de l'abonnement qui le paie
    (son demandeur, ou le membre qui a prêté le sien au pool de l'org).

    Ce que le worker reçoit en plus : `sandbox_id`. Jamais de clé, jamais de
    session — il exécutera le programme officiel DANS ce sandbox, qui lit la
    sienne tout seul.

    ⚠️ Un porteur sans connexion ARRÊTE le travail, raison écrite. Le remettre en
    file le ferait reprendre par le worker suivant, indéfiniment, sans que
    personne n'apprenne pourquoi — la leçon de `_refuser_sans_cle`.
    """
    # L'abonnement qui PAIE : celui que la réservation a choisi — le demandeur en
    # mode personnel, un prêteur du pool sinon (`db.porteur_du_forfait`).
    porteur = db.porteur_du_forfait(job)
    ligne = (user_subscriptions.get_subscription(porteur, famille) or {}) if porteur else {}
    # La voie que la RÉSERVATION a prise (`_plateforme.pool`), jamais recalculée : le
    # pool complète, et la voie se décide par travail (`sql_par_le_pool`).
    pool = db.par_le_pool(job)
    if job.get("fleet_id") and not pool:
        # Un passage armé pendant le pool, dont l'org est repassée en personnel :
        # servi ici, il ferait payer le forfait de son créateur pour le travail de
        # tous — la faute que la pose refuse (`subscription_personal_only`).
        return _refuser_sans_cle(job, appelant,
                                 _abonnement._FLOTTE_HORS_POOL.format(famille=famille))
    servable, statut, sandbox = _abonnement.servable_de_la_ligne(ligne)
    if not servable and (pool or _abonnement.reparable(statut, sandbox)):
        # ⚠️ RENDU à la file, pas arrêté (21/09/2026) : la personne doit se
        # reconnecter, et ce n'est pas la faute du travail. La réservation saute
        # déjà ces personnes — n'arrive ici que la course où l'état a changé entre
        # la prise et cette garde. `delegation_refusee` reste le champ que le
        # worker DÉPLOYÉ sait lire : il n'exécute pas et ne conclut pas.
        # En POOL, toujours rendu : un autre prêteur servira le travail.
        raison = _abonnement.raison_de_l_attente(famille, statut, pool=pool)
        db.rendre_a_la_file(job["id"], appelant, raison)
        return {**job, "delegation_refusee": raison, "delegated_token": None}
    if not servable:
        return _refuser_sans_cle(job, appelant,
                                 _abonnement.raison_du_refus(famille, statut))
    # Trace de REMISE, comme pour une clé : qui, quelle org, quel sandbox, et de qui
    # est le forfait. Elle ne peut rien révéler d'un secret — il n'y en a pas ici.
    logger.info("abonnement `%s` servi à %s pour l'org %s (travail %s, sandbox %s%s)",
                famille, appelant, job.get("org_id"), job.get("id"), sandbox,
                ", prêté au pool" if pool else "")
    # La sonde ne vaut que pour une pause : servi alors qu'elle est EN VIGUEUR, c'est
    # qu'elle est ancienne et de fenêtre inconnue (`pause_en_vigueur(ab, sonde)`), et
    # le worker doit la sonder avant de lancer. Sur un forfait connecté, le run
    # rapportera lui-même : une sonde n'y coûterait que des jetons.
    if _reprise_future(ligne) is not None:
        return {**job, "sandbox_id": sandbox, "forfait_a_sonder": True}
    return {**job, "sandbox_id": sandbox}


_SANS_DEPOT = (
    "ce worker ne nomme aucun fournisseur de clé : il tournerait sur la clé de son "
    "propre environnement, et cette organisation exige que ses agents tournent sur "
    "la sienne. Travail non exécuté — il doit être servi par un worker qui nomme son "
    "dépôt (OTO_RUNNER_PROVIDER / OTO_RUNNER_OPENAI_BASE côté oto-runner).")


_SANS_MODELE = (
    "ce travail ne déclare aucun modèle : il tournerait sur le modèle du worker, pas "
    "sur la clé de modèle de son organisation. Travail non exécuté. Déclare un modèle "
    "sur l'agent (`model`), dépose la clé de ce fournisseur si l'organisation doit "
    "tourner sur la sienne, puis rallume l'agent.")

_SANS_CLE_DEPOSEE = (
    "ce travail demande un modèle `{depot}`, et les agents `{depot}` ne tournent que "
    "sur la clé déposée par l'organisation — celle-ci n'en a pas déposé. Travail non "
    "exécuté. Dépose une clé `{depot}` (Connecteurs, ou la fiche de l'agent), puis "
    "rallume l'agent.")


def _refuser_sans_cle(job: dict, appelant: str, raison: str) -> dict:
    """Arrête le travail pour de bon, raison écrite, et le rend au worker marqué
    comme tel.

    ⚠️ `delegation_refusee` est le champ que le worker DÉPLOYÉ sait déjà lire :
    il n'exécute pas, ne conclut pas (le travail est déjà `failed`), et journalise
    la raison. Un champ neuf aurait demandé un runner neuf pour que la garde morde ;
    celui-ci la fait mordre dès le déploiement du backend. Le jeton délégué émis
    juste avant (`_delegue`) est RETIRÉ de la réponse : un travail qui ne tournera
    pas n'a rien à faire d'un pouvoir d'agir, même borné au bail.

    ⚠️ Pas de retour en file : réessayer rejouerait le même verdict, et un travail
    refusé en boucle ne dit rien de plus la troisième fois que la première.
    """
    db.arreter_definitivement(job["id"], appelant, raison)
    logger.warning("travail %s (org %s) ARRÊTÉ sans clé de modèle : %s",
                   job.get("id"), job.get("org_id"), raison)
    return {**job, "delegation_refusee": raison, "delegated_token": None}


def _doute_du_coffre(job: dict, appelant: str, depot: str,
                    raison: Optional[str] = None) -> None:
    """Le coffre n'a pas répondu à la réservation : on ne sait ni quelle clé sert
    cette org, ni si elle en a une. Rien n'est servi — notre clé pourrait payer sans
    mesure — et rien n'est arrêté — une raison écrite sur un doute serait fausse. Le
    travail retourne à la file sans compter la tentative.

    `raison` : ce que le doute porte, quand on en sait plus (`_CLE_DEPOSEE_ILLISIBLE`)."""
    db.rendre_a_la_file(job["id"], appelant, raison or
                        f"coffre illisible à la réservation (clé `{depot}`) — réessai")
    logger.warning("travail %s (org %s) : coffre illisible pour la clé `%s` — rendu à la "
                   "file, ni servi ni arrêté", job.get("id"), job.get("org_id"), depot)
    return None


_CLE_DEPOSEE_ILLISIBLE = (
    "clé `{depot}` déposée par l'organisation, mais illisible au coffre à la "
    "réservation — travail ni exécuté ni arrêté, réessai. Si cela dure, redépose la "
    "clé (Connecteurs).")


_SOUS_LE_PLANCHER = (
    f"{budget.INSUFFISANT} : le solde de crédits ne couvre pas le démarrage d'un agent "
    "sur la clé partagée. Travail non exécuté. Recharge des crédits, ou dépose ta propre "
    "clé (Connecteurs) ; {suite}")
_SUITE_RECURRENT = "l'agent repartira à sa prochaine échéance."
_SUITE_PONCTUEL = "puis relance ce travail."
_SOLDE_ILLISIBLE = (
    f"{budget.INDISPONIBLE} : solde de crédits illisible à la réservation — travail ni "
    "exécuté ni arrêté, réessai.")


def _agent_a_horloge(job: dict) -> bool:
    """Le travail est-il l'occurrence d'un agent qui REPART seul — un déclencheur
    programmé (`kind` `schedule`, un `cron`), allumé ? Le `trigger_id` de la charge n'est
    posé que par le serveur (horloge, webhook, `oto_trigger op=run`) : c'est lui qui dit
    de quel agent ce travail est un run. Un webhook, un appel direct, un agent éteint ou
    retiré : ponctuel — rien ne le relancera à une échéance."""
    tid = (job.get("payload") or {}).get("trigger_id")
    if not str(tid or "").isdigit():
        return False
    t = db.get_trigger(int(tid), job["org_id"])
    return bool(t and (t.get("kind") or "schedule") == "schedule" and t.get("cron")
                and t.get("enabled"))


def _sous_le_plancher(job: dict) -> str:
    """La raison du plancher : « repartira à sa prochaine échéance » n'est vraie que pour
    un agent à horloge ; un travail ponctuel doit être relancé."""
    return _SOUS_LE_PLANCHER.format(
        suite=_SUITE_RECURRENT if _agent_a_horloge(job) else _SUITE_PONCTUEL)


_ATTEND_UN_WORKER_QUI_MESURE = (
    "en attente d'un worker qui mesure chaque appel au modèle : la clé partagée n'est "
    "servie qu'à lui.")


_SANS_PORTEUR = (
    "ce travail ne nomme personne — il a été enfilé avant que la file retienne "
    "son demandeur (02/09/2026). Le worker n'a pas d'identité propre à lui "
    "prêter : reprogramme-le, il partira au nom de qui le demande.")


#: Dernière cause signalée par org, pour ne pas répéter le même échec à chaque
#: sondage — `{org_id: (cause, instant)}`. En mémoire de process : au pire un
#: redémarrage rejournalise une fois, ce qui est le bon défaut.
_CAMPAGNE_MUETTE: dict[int, tuple[str, float]] = {}


def _produire_pour_une_campagne(org_id: Optional[int], bail_s: int) -> Optional[str]:
    """Fabrique UN travail pour une campagne en cours de l'org, s'il y en a une.

    Un seul, et sans le réserver : l'appelant re-sonde juste après et le prendra
    comme n'importe quel autre. Deux workers qui produisent en même temps ne se
    gênent pas — chacun fabrique le sien, et c'est exactement le parallélisme
    voulu — **jusqu'au plafond `workers` de la campagne** (#907, oto#245) : jamais
    plus de `workers` travaux en cours. Jusqu'au 23/09/2026 ce texte disait « ce
    qui les borne est leur nombre, pas un réglage », et c'était vrai : le champ
    était ignoré. L'enfilage revérifie la borne sous verrou
    (`enqueue_job(..., seulement_si_servable=True)`) ; `None` = un sondage
    concurrent a pris la dernière place, rien n'est produit.

    ⚠️ L'identité du travail est celle de QUI A DÉCLARÉ la campagne
    (`fleet["sub"]`), jamais celle du worker qui sonde. C'est ce que `_delegue`
    lira ensuite pour émettre le jeton : un travail produit ici agit au nom du
    demandeur, comme un travail enfilé à la main.

    Fail-open et tracé : une campagne illisible ne doit pas casser le sondage de
    tous les workers de l'org. Le passage attend simplement le sondage suivant.

    ⚠️ DETTE CONNUE, nommée ici parce que c'est ici qu'on la cherchera : la borne
    de **dépense cumulée** (`max_tokens` d'une campagne) n'est pas appliquée.
    L'ancien ordonnanceur la tenait ; ce chemin ne la tient pas encore. Deux des
    trois bornes le sont — `max_rows` dans la sélection, les échecs consécutifs
    juste au-dessus — la troisième non.

    Ce qu'elle coûterait mal faite : sommer la consommation des travaux d'une
    campagne À CHAQUE SONDAGE, c'est-à-dire en boucle sur chaque worker. C'est
    la même erreur que le comptage de lignes restantes qu'on a écarté plus haut,
    et elle se paierait au même endroit — le chemin le plus fréquent de la
    plateforme. Il faut donc un compteur tenu à l'écriture, pas une somme à la
    lecture ; ce n'est pas un oubli, c'est un travail qui n'est pas fait.

    Tant qu'elle manque, une campagne peut dépasser son budget déclaré.

    ⚠️ Ce texte pointait « la garde d'armement ci-dessus », c'est-à-dire
    l'interrupteur d'environnement retiré le 08/09/2026 — il désignait donc une
    protection qui n'existe plus. Ce qui borne réellement le risque, et qui n'a
    jamais dépendu d'un réglage :

    - **une campagne ne produit rien tant que personne ne l'a ARMÉE** — la
      clause `status IN ('armed', 'running')` de `campagne_a_servir`. C'est la
      seule garde d'entrée, et c'est un geste humain explicite ;
    - `max_rows`, compté dans la même requête, borne le NOMBRE de travaux ;
    - `max_consecutive_failures` arrête une campagne qui échoue en boucle.

    Ce qui n'est pas borné reste la SOMME sur la campagne. Mais le pire cas est
    calculable, et le dire évite de présenter « non borné » là où il ne l'est
    pas : `max_tokens_per_row` part avec le travail (`payload["max_tokens"]`,
    plus bas) et l'AGENT l'applique — il s'arrête dessus, `stopped=max_tokens`.
    Une campagne qui le déclare est donc bornée à `max_rows × max_tokens_per_row`
    exactement. Sans lui, il ne reste que le plafond de TOURS (`max_steps`), une
    borne en tours et non en jetons : c'est là, et seulement là, que « non
    borné » est vrai.

    ⚠️ Le parallélisme ne multiplie rien : la borne est `max_rows`, jamais
    `max_rows × workers`. Plus de travailleurs concentrent la dépense dans le
    temps, ils ne l'augmentent pas.
    """
    try:
        # ⚠️ AVANT de servir : arrêter celles qui échouent en boucle. L'ordre
        # compte — une campagne épuisée doit être arrêtée, pas seulement sautée,
        # sinon elle reste `running` sans avancer et son état ment.
        for fid in db.arreter_campagnes_epuisees(org_id):
            logger.warning("campagne %s arrêtée : ses derniers travaux ont tous "
                           "échoué (max_consecutive_failures)", fid)
        # ⚠️ Et AVANT de servir aussi : accuser les arrêts devenus effectifs.
        # `op=stop` met en `stopping` ; c'était l'ordonnanceur qui accusait, et
        # il n'existe plus. Sans ce geste, un arrêt demandé n'est jamais un
        # arrêt constaté et la campagne reste `stopping` pour toujours — un
        # état qui ment, exactement ce que la ligne au-dessus évite pour les
        # campagnes épuisées.
        for fid in db.accuser_arrets_effectifs(org_id):
            logger.info("campagne %s arrêtée pour de bon : plus aucun travail "
                        "en attente ni en cours", fid)
        # ⚠️ Et la fin naturelle : une campagne à sa borne `max_rows` n'est plus
        # servie, mais sans ce geste elle resterait `running` pour toujours — et
        # qui attend qu'elle finisse (un agent quotidien) attendrait aussi.
        for fid in db.clore_campagnes_terminees(org_id):
            logger.info("campagne %s terminée : max_rows atteint, plus aucun "
                        "travail en attente ni en cours", fid)
        f = db.campagne_a_servir(org_id, _ordre_de_service.ordonner)
        if not f or not f.get("sub"):
            return None
        # Une campagne d'avant #1067 garde le NOM de son tableau : son identifiant est
        # posé ici, au premier travail, avant que la consigne ne le cite.
        f = _lignes_reservables.fixer_le_tableau(f)
        message = runner_consigne.composer(f)
        travail = db.enqueue_job(
            # L'org de la CAMPAGNE, jamais celle de l'appelant : un worker de
            # plateforme n'en a pas, et un travail sans org serait orphelin.
            f["org_id"], "start",
            payload={"procedure": f["procedure"], "tools": list(f.get("tools") or ()),
                     "project_id": f.get("project_id"), "org_id": f["org_id"],
                     "namespace": f.get("namespace"),
                     # L'IDENTIFIANT du tableau (oto#160), celui que la campagne garde
                     # depuis sa déclaration (#1067) : un écran qui résoudrait un nom le
                     # résoudrait avec SON demandeur — celui qui lit.
                     "datastore_id": _lignes_reservables.cle_de_campagne(f),
                     "fleet": f.get("label"),
                     "max_steps": f.get("max_steps"),
                     "max_tokens": f.get("max_tokens_per_row"),
                     **_limites_du_run.charge(None, f.get("max_run_seconds")),
                     # Ce que l'agent lit des outils, si la campagne l'a déclaré (oto#241) ;
                     # sinon rien ne part et le worker garde son défaut.
                     **({"descriptions_outils": f["descriptions_outils"]}
                        if f.get("descriptions_outils") else {}),
                     # Le contexte d'exécution déclaré par le passage. `None` =
                     # on n'envoie rien et le fournisseur applique son défaut —
                     # c'est le comportement d'avant, et il reste possible.
                     "temperature": f.get("temperature"),
                     # Le modèle déclaré par le passage, et la famille qui route le
                     # travail. Une flotte sans modèle — ou d'un modèle hors
                     # catalogue — n'envoie rien : n'importe quel worker la sert.
                     **runner_models.charge(f.get("model")),
                     "input": message,
                     "label": f"flotte {f.get('namespace')} — {f['procedure']}"},
            fleet_id=f["id"], sub=f["sub"], seulement_si_servable=True)
        if travail is None:
            return None
        db.marquer_demarree(f["id"])
        return None
    except Exception as e:
        # ⚠️ UNE fois par cause, pas à chaque sondage. Ce chemin tourne en boucle
        # sur chaque worker : journaliser sans retenue noierait le journal sous
        # des milliers de lignes identiques, et un journal noyé ne se lit pas —
        # c'est l'autre façon de ne rien dire. On répète quand la cause change,
        # ou après un quart d'heure, pour qu'une panne qui dure reste visible
        # sans devenir du bruit.
        cause = f"{type(e).__name__}: {e}"
        # `org_id=None` = sondage d'un worker de plateforme : une seule entrée
        # pour toutes les orgs, donc une cause peut en masquer une autre un
        # quart d'heure. Assumé : le worker reçoit la sienne à chaque sondage.
        vu, quand = _CAMPAGNE_MUETTE.get(org_id, (None, 0.0))
        if cause != vu or time.monotonic() - quand > 900:
            _CAMPAGNE_MUETTE[org_id] = (cause, time.monotonic())
            logger.warning("campagne : production de travail impossible pour l'org %s "
                           "— les passages de cette org n'avancent plus", org_id,
                           exc_info=True)
        # Rendue à l'appelant, pas seulement journalisée ici : un journal serveur
        # n'est lu que par qui SAIT déjà qu'il y a un problème. Le worker, lui,
        # reçoit la réponse à chaque sondage — c'est le seul endroit où la panne
        # atteint quelqu'un qui l'attendait.
        return cause[:300]


def _delegue(job: dict, bail_s: int, claimant: str) -> dict:
    """Le travail, augmenté du moyen d'agir AU NOM de son porteur.

    ⚠️ Le worker n'est pas un pouvoir : c'est **un client MCP ordinaire qui porte
    l'identité du demandeur** (arbitrage du 02/09). Rien ici ne lui donne un droit
    propre — on lui remet un jeton au nom de quelqu'un d'autre, borné à la durée
    du bail, et il s'en sert comme n'importe quel client.

    ⚠️ La validité se vérifie ICI, à la réservation, et **une seule fois** : un
    travail long continue avec un droit retiré en cours de route, c'est assumé.
    """
    porteur = job.get("sub")
    if not porteur:
        # ⚠️ Le worker est un SERVEUR DE BOUCLES AGENTIQUES : chaque boucle
        # impersonne son user, et lui n'a **aucune identité métier**. Un travail
        # sans porteur n'a donc personne à impersonner.
        #
        # Le servir nu — ce qu'on faisait pour les travaux d'avant le 02/09 —
        # faisait retomber la boucle sur le jeton DU WORKER : un agent qui agit
        # au nom du compte qui héberge le runner, et tout ce qu'il écrit signé
        # par lui. Le défaut est silencieux par construction : les écritures
        # aboutissent, seule l'attribution est fausse. On refuse, et on le DIT.
        db.refuser_pour_identite(job["id"], claimant, _SANS_PORTEUR)
        return {**job, "delegation_refusee": _SANS_PORTEUR}
    org_id = job.get("org_id")
    raison = _identite_invalide(porteur, org_id) if org_id else None
    if raison:
        # ⚠️ Le travail est ARRÊTÉ ET LA RAISON EST ÉCRITE. Le relâcher en silence
        # le ferait reprendre par le worker suivant, indéfiniment : une file qui
        # tourne sans jamais aboutir, et rien pour dire pourquoi. Un agent dont
        # l'identité n'est plus valide s'arrête EN LE DISANT.
        db.refuser_pour_identite(job["id"], claimant,
                                 f"identité invalide — {raison}")
        return {**job, "delegation_refusee": raison}
    # ⚠️ Purger AVANT d'émettre : le nettoyage est amorti sur l'usage, sans
    # tâche de fond à faire vivre. Un jeton mort est inutilisable, et
    # l'accumulation est mécanique — un par travail exécuté.
    db.purger_delegations_expirees(porteur)
    # Le jeton porte son TRAVAIL et l'org de ce travail : le porteur peut appartenir à
    # plusieurs orgs, son travail n'en a qu'une, et toute résolution hors d'elle est
    # refusée (`verrou_org.py`). Un travail sans org est borné à la portée personnelle.
    job["delegated_token"] = db.create_api_token(
        porteur, label=f"runner job {job['id']}",
        ttl_seconds=bail_s + db.MARGE_JETON_DELEGATION_S, kind="delegation",
        job_id=job.get("id"), verrou_org=True,
        verrou_org_id=int(org_id) if org_id is not None else None)
    return job


def charge_et_modele(ctx: ResolvedCtx, inp: JobsInput) -> Optional[dict]:
    """La charge d'un travail enfilé à la main, son modèle mis en règle.

    ⚠️ **La famille se DÉDUIT, elle ne se déclare pas.** C'est elle qui route le
    travail (`claim_next_job`) : une famille posée par l'appelant enverrait un
    modèle Mistral à un worker Anthropic, qui le recevrait comme une commande
    valide. Celle qui arrive dans la charge est donc retirée, et recalculée depuis
    `model` — refusé s'il est hors catalogue.

    ⚠️ **Un `continue` reprend le modèle de son run**, quel que soit celui qu'on lui
    passe : un fil ouvert sur une voie ne se poursuit pas sur une autre. Un run
    démarré sans modèle se poursuit sans modèle — la charge d'avant, à l'octet."""
    if inp.payload is None and inp.kind != "continue":
        return None
    charge = {k: v for k, v in (inp.payload or {}).items()
              if k not in ("model", "model_family")}
    # L'allowlist au canonique : c'est elle que le worker confronte, EXACTEMENT, au
    # `tools/list` qu'on lui sert en canonique (`tool_alias.prefix_for`).
    if "tools" in charge:
        charge["tools"] = tool_alias.canonical_names(charge["tools"], ctx.sub)
    if inp.kind == "continue":
        charge.update(db.modele_du_run(inp.run_id, ctx.org_id))
    else:
        model = (inp.payload or {}).get("model")
        if model is not None and not isinstance(model, str):
            raise AuthzDenied(400, "invalid_model", "`payload.model` est un nom de modèle")
        _modele.famille_declaree(model)
        charge.update(runner_models.charge(model))
        # Le QUATRIÈME chemin de pose (revue du 23/09/2026) : un travail enfilé à la
        # main sur un modèle d'abonnement passe la même garde qu'un agent posé —
        # sinon une flotte, ou un membre sans l'option, contournait les trois autres.
        _abonnement.exiger_a_la_pose(ctx.sub, None, charge.get("model_family"),
                                     flotte=inp.fleet_id is not None, org_id=ctx.org_id)
    return charge if (charge or inp.payload is not None) else None


#: Ce qu'un worker sait faire — et rien d'autre. Enfiler, lister, lire un
#: travail sont des gestes d'organisation : ils exigent une org, et un worker
#: n'en a pas.
_VERBES_DU_WORKER = frozenset({"claim", "bind_run", "extend", "complete", "reserve",
                               "meter"})


def _exige_flotte_servie(statut: Optional[str]) -> None:
    """Refuse de rattacher un travail à une flotte qui ne le servira pas.

    ⚠️ L'APPARTENANCE d'abord, pas seulement l'existence. La FK vers
    `runner_fleets` garantit que la flotte existe — elle ne dit rien de QUI elle
    est. Sans cette vérification, un travail se rattacherait à la flotte d'une
    autre org et ferait entrer son coût et son avancement dans l'état d'un
    passage étranger : une fuite d'observabilité, et un état faux des deux côtés.
    Même 404 sans oracle que le gate propriétaire d'un run.

    ⚠️ Puis l'ÉTAT (oto-backend#996, 18/09/2026). L'enfilement ne regardait que
    l'appartenance : un ordonnanceur dont l'armement avait été refusé
    (`no_runner_armed`, ou n'importe quel refus de `launch`) enfilait quand même
    sur une flotte restée `draft`. Ces travaux tournaient, dépensaient — et
    `op=stop` les refusait (`not_stoppable` : on n'arrête qu'une flotte
    `armed`/`running`). Des exécutions qu'aucun geste ne peut plus arrêter : on
    refuse donc de les créer, hors des états que `stop` sait arrêter.
    """
    if statut is None:
        raise AuthzDenied(404, "fleet_not_found", "automatisation inconnue")
    if statut not in db.STATUTS_QUI_SERVENT:
        raise AuthzDenied(
            409, "fleet_not_serving",
            f"cette automatisation est `{statut}` : elle n'accepte une exécution que "
            "lorsqu'elle est armée (`armed`) ou en cours (`running`) — seuls états "
            "que `stop` sait arrêter. Une exécution ajoutée maintenant tournerait "
            "hors de portée de tout arrêt. Arme-la (`op=launch`, puis `op=take` si "
            "c'est ton ordonnanceur qui la prend) et reprends l'enfilement — une "
            "automatisation `stopping` doit d'abord atteindre `stopped` —, ou "
            "déclares-en une autre.")


def _resume(ctx: ResolvedCtx, jours: int) -> dict:
    """`op=summary` : la page Agents en UN appel — par agent, les travaux par heure
    et par statut, le total, la dernière panne (oto-front « optimiser le
    chargement », 17/10/2026). Les cases de l'écran se dessinent depuis ces
    heures ; le détail d'un travail se lit toujours par `list`/`get`.

    ⚠️ Filtré par VISIBILITÉ, comme `runner.triggers op=list` : un agent que
    l'appelant ne voit pas n'apparaît pas, pas même par son nombre de travaux."""
    cases, pannes = db.resume_des_declencheurs(ctx.org_id, jours)
    ids = {int(r["trigger_id"]) for r in cases}
    agents = [t for t in db.list_triggers(ctx.org_id) if int(t["id"]) in ids]
    vus = {i for i, n in _acces_agent.niveaux(ctx.sub, ctx.org_id, agents).items() if n}
    par_agent: dict[int, dict] = {}
    for r in cases:
        tid = int(r["trigger_id"])
        if tid not in vus:
            continue
        a = par_agent.setdefault(tid, {"trigger_id": tid, "total": 0, "hours": [], "last_failure": None})
        a["total"] += int(r["n"])
        # Les dates arrivent DÉJÀ en texte (« AAAA-MM-JJ HH:MM:SS », UTC naïf) : le
        # `row_factory` du pool (`_str_dict_row`) les normalise, comme pour toute la
        # base. Un `.isoformat()` ici tombait sur `str` — un 500 sur chaque org.
        a["hours"].append({"hour": r["heure"], "status": r["status"], "count": int(r["n"])})
    for r in pannes:
        tid = int(r["trigger_id"])
        if tid in par_agent:
            par_agent[tid]["last_failure"] = {"due_at": r["due_at"],
                                              "error": r["last_error"]}
    for a in par_agent.values():
        a["hours"].sort(key=lambda h: h["hour"])

    # Le temps gagné : les runs conclus `done` de chaque agent vu, leurs appels du
    # journal rangés par type de résultat (`time_saved.py`), aux minutes de l'org. Un
    # run sans appel au journal n'est PAS compté — il est dit, pas estimé.
    minutes = time_saved.standards(db.minutes_gagnees_de_l_org(ctx.org_id))
    conclus = [r for r in db.outils_des_runs_conclus(ctx.org_id, jours)
               if int(r["trigger_id"]) in par_agent]
    par_run: dict[str, list] = {}
    for c in db.appels_des_runs([r["run_id"] for r in conclus if r.get("run_id")]):
        par_run.setdefault(c["run_id"], []).append(time_saved.Call(
            tool=c["tool"], op=c["op"], ok=bool(c["ok"]), key=c["cle"], day=c["jour"]))
    runs_par_agent: dict[int, list] = {}
    sans_journal: dict[int, int] = {}
    for r in conclus:
        tid = int(r["trigger_id"])
        appels = par_run.get(r.get("run_id") or "")
        if appels:
            runs_par_agent.setdefault(tid, []).append(appels)
        else:
            sans_journal[tid] = sans_journal.get(tid, 0) + 1
    tallies: dict[int, dict] = {}
    for tid, runs in runs_par_agent.items():
        gagne, tallies[tid] = time_saved.for_agent(runs, minutes)
        par_agent[tid]["time_saved_minutes"] = gagne
    for tid, a in par_agent.items():
        tally = tallies.get(tid, {})
        a["time_saved_minutes"] = round(a.get("time_saved_minutes", 0.0), 1)
        a["time_saved"] = [
            {"kind": k, "label": time_saved.KINDS[k]["label" if tally[k]["count"] == 1 else "plural"],
             "count": tally[k]["count"], "minutes_each": minutes[k],
             "minutes": round(tally[k]["minutes"], 1)}
            for k in time_saved.KINDS if k in tally
        ]
        a["time_saved_not_counted"] = (
            [{"reason": "no_logged_calls", "runs": sans_journal[tid]}] if sans_journal.get(tid) else [])
    depuis = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - jours * 86400))
    return {"summary": sorted(par_agent.values(), key=lambda a: a["trigger_id"]), "since": depuis,
            "time_standards": minutes}


def _jobs(ctx: ResolvedCtx, inp: JobsInput) -> dict:
    # Deux principaux, deux files. Un MEMBRE agit sur la file de SON org, et
    # doit en avoir une. Un WORKER de plateforme n'en a aucune : il sonde, et
    # c'est le backend qui choisit, parmi toutes les orgs, le travail à lui
    # commander — il ne connaît que les verbes du bail, jamais ceux qui
    # déclarent ou consultent une file (« tout doit être paramétrique, en base
    # et depuis la commande du backend », 09/09/2026).
    if ctx.platform_worker:
        if inp.op not in _VERBES_DU_WORKER:
            raise AuthzDenied(403, "worker_verbs_only",
                              f"un worker de plateforme ne fait que "
                              f"{', '.join(sorted(_VERBES_DU_WORKER))} — "
                              f"`{inp.op}` est un geste d'organisation.")
    elif not ctx.org_id:
        raise AuthzDenied(400, "org_required",
                          f"`{inp.op}` porte sur la file d'une organisation — "
                          "nomme-la (`X-Oto-Org` côté REST, `_org` côté MCP), "
                          "ou choisis-en une avec oto_use_org.")

    if inp.op == "enqueue":
        if inp.kind is None:
            raise AuthzDenied(400, "missing_fields", "enqueue exige `kind`")
        if inp.kind == "continue" and not inp.run_id:
            raise AuthzDenied(400, "missing_fields",
                              "un job `continue` exige `run_id` — quel fil reprendre ?")
        if inp.kind == "start" and not inp.payload:
            raise AuthzDenied(400, "missing_fields",
                              "un job `start` exige `payload` (au moins la procédure à charger)")
        # ⚠️ Le `trigger_id` d'une charge dit de quel AGENT ce travail est un run : le
        # serveur seul le pose (horloge, webhook, `oto_trigger op=run`). Reçu ici, il
        # rangerait un travail quelconque sous l'agent d'un autre — et, par lui, le
        # passage qu'il déclarerait (`runner_fleets._agent_declarant`).
        if "trigger_id" in (inp.payload or {}):
            raise AuthzDenied(400, "trigger_id_server_set",
                              "`payload.trigger_id` est posé par le serveur pour le run "
                              "d'un agent : lance un agent par `oto_trigger op=run`.")
        if inp.run_id:
            # Le gate propriétaire tient CÔTÉ SERVEUR, pas dans le séquencement de
            # l'UI : enfiler un `continue` sur le run d'autrui ferait continuer son
            # fil par le worker, avec les droits du run — un trou d'autz, pas un
            # détail. Même règle et même 404 sans oracle que l'append du fil (R1).
            head = db.get_run_head(inp.run_id)
            if not head or head.get("sub") != ctx.sub:
                raise AuthzDenied(404, "run_not_found", "run inconnu")
        # ⚠️ L'identité vient de l'ÉTAT SERVEUR (`ctx.sub`), jamais d'un champ
        # d'entrée : un travail dont l'appelant choisirait le porteur serait une
        # usurpation en une ligne de JSON. Le paramétrage vers un autre membre —
        # prévu par la direction du 02/09 — passera par une garde
        # d'appartenance, pas par la confiance faite au corps de la requête.
        if inp.fleet_id is None:
            res = db.enqueue_job(ctx.org_id, inp.kind,
                                 payload=charge_et_modele(ctx, inp),
                                 run_id=inp.run_id, max_attempts=inp.max_attempts,
                                 sub=ctx.sub)
        else:
            # La lecture de la flotte et l'INSERT partagent UNE transaction, sous
            # verrou partagé (`verrouiller_la_flotte`) : un `stop` concurrent passe
            # avant ou après l'enfilement, jamais entre les deux. La charge se
            # compose APRÈS la garde, comme avant elle : l'ordre des refus
            # (`fleet_not_found` avant `invalid_model`) ne bouge pas.
            with db._connect() as conn:
                _exige_flotte_servie(db.verrouiller_la_flotte(
                    conn, inp.fleet_id, ctx.org_id))
                res = db.enqueue_job(ctx.org_id, inp.kind,
                                     payload=charge_et_modele(ctx, inp),
                                     run_id=inp.run_id, max_attempts=inp.max_attempts,
                                     fleet_id=inp.fleet_id, sub=ctx.sub, conn=conn)
        return {"id": res["id"], "status": res["status"], "due_at": str(res["due_at"]),
                "fleet_id": res.get("fleet_id"), "sub": res.get("sub")}

    if inp.op == "claim":
        if inp.org_key_only and not inp.provider:
            raise AuthzDenied(
                400, "org_key_only_without_provider",
                "`org_key_only` exige `provider` : un worker qui ne tourne que sur "
                "la clé de l'organisation doit nommer QUEL dépôt il consomme — "
                "sans lui, il n'y a aucune clé à attendre et rien à servir.")
        ferme = _engine_ferme(ctx, inp)
        bail = max(30, min(inp.lease_seconds, 3600))
        # ⚠️ Un worker d'ABONNEMENT ne prend QUE sa famille, qu'il l'ait demandé ou
        # non (revue du 21/09/2026). Il n'exécute rien lui-même : tout part dans le
        # sandbox du demandeur. Un travail SANS famille — l'agent historique
        # posé sans modèle — n'a aucun sandbox : servi à ce worker, il échoue à
        # coup sûr, tentative après tentative. Le drapeau `org_key_only` le
        # garantissait déjà… à condition que l'unité systemd le pose. Une garde qui
        # dépend d'une variable d'environnement bien écrite n'en est pas une.
        famille_seule = inp.org_key_only or _abonnement.est_abonnement(inp.provider)
        # ⚠️ Un MEMBRE ne réserve que SES travaux (10/10/2026). Le travail réservé
        # revient avec un jeton de délégation au nom de SON porteur (`_delegue`) :
        # un membre qui réservait le travail d'un collègue en recevait un qui agit au
        # nom du collègue — usurpation de compte par un simple `claim`, le collègue
        # fût-il admin de l'org. Le worker de PLATEFORME (secret de machine) garde la
        # file entière : c'est son rôle, et il n'a pas d'identité à confondre.
        porteur = None if ctx.platform_worker else ctx.sub
        job = db.claim_next_job(ctx.org_id, ctx.sub, lease_seconds=bail,
                                depot=inp.provider, famille_seule=famille_seule,
                                org_ids=inp.org_ids, ferme=ferme, porteur=porteur,
                                sonde=inp.forfait_sonde)
        if job is None:
            # ⚠️ La file vide n'est pas la fin de l'histoire : une CAMPAGNE en
            # cours est une règle qui produit des travaux, et c'est ici qu'on
            # l'évalue. Le worker ne connaît pas cette notion — il demande du
            # travail, on lui en fabrique un.
            #
            # Ce que ça remplace : un ordonnanceur externe qu'un humain lançait
            # à la main sur la machine, qui prenait la campagne, la découpait,
            # maintenait des travaux en vol et battait pour dire qu'il vivait.
            # Rien de tout cela n'a de raison d'être si le sondage du worker
            # suffit à faire avancer le passage — et il suffit, puisqu'il a
            # déjà lieu en boucle.
            panne = _produire_pour_une_campagne(ctx.org_id, bail)
            job = db.claim_next_job(ctx.org_id, ctx.sub, lease_seconds=bail,
                                    depot=inp.provider,
                                    famille_seule=famille_seule,
                                    org_ids=inp.org_ids, ferme=ferme,
                                    porteur=porteur, sonde=inp.forfait_sonde)
            if job is None and panne:
                # « Rien à faire » et « je n'ai pas pu regarder » ne se disent
                # pas de la même façon. Les confondre a coûté des jours de
                # sondage à vide sans que personne ne le voie (07/09/2026).
                return {"job": None, "campaign_error": panne}
        if job is None and ctx.platform_worker and inp.provider:
            # ⚠️ REPLI ABONNEMENT ÉPUISÉ → clé API (OTO-130, 27/09/2026) : un
            # travail d'abonnement en pause (jamais needs_login/disconnected,
            # jamais un pool vide) rejoue sur la clé API de son org plutôt que
            # d'attendre — seulement si l'org l'a CHOISI et que SA clé paie
            # (`repli_disponible` revérifie avant de prendre). Réservé au worker
            # de PLATEFORME, même garde que le sandbox d'abonnement lui-même : ce
            # chemin traverse une famille que le dépôt de ce worker ne nomme pas.
            # ⚠️ Sans `forfait_sonde` : le repli sur la clé de l'org lit la pause
            # STOCKÉE, quel que soit le worker — une pause ancienne qu'une sonde
            # revérifiera reste une pause pour le repli (décision du 22/10/2026).
            job = db.repli_disponible(ctx.org_id, inp.org_ids, ctx.sub,
                                      inp.provider, _abonnement.DEFAUT_LIMITE_PCT,
                                      lease_seconds=bail, ferme=ferme)
        if job is None:
            return {"job": None}
        # La charge servie est composée AVANT la délégation : tout ce qui se décide
        # ensuite voit l'org et les outils que l'agent recevra.
        return {"job": _avec_cle(
            _delegue(_charge_servie(job), bail, ctx.sub), inp.provider,
            ctx.sub, worker=ctx.platform_worker, org_key_only=inp.org_key_only,
            metre=inp.meters_usage, ferme=ferme)}

    if inp.op == "summary":
        return _resume(ctx, inp.days)

    if inp.op == "list":
        # Surveillance (page Automatisations) : lecture org-scopée, jamais un
        # geste — la liste rend de quoi écarter, le détail se demande par get.
        #
        # #469 : la page DIT ce qu'elle laisse dehors. `total` compte la file sous
        # les mêmes filtres (il ne bouge pas d'une page à l'autre : c'est le
        # dénominateur d'un bilan) ; `next_cursor` dit comment lire la suite. Sans
        # eux, un bilan de vague lisait `len(jobs)` comme le compte de la file et
        # sous-déclarait — un relevé tronqué rend MOINS que la réalité, jamais plus.
        avant = _depuis_curseur(inp.cursor) if inp.cursor else None
        jobs = db.list_jobs(ctx.org_id, status=inp.status, limit=inp.limit,
                            before_id=avant, source=inp.source,
                            fleet_id=inp.fleet_id, trigger_id=inp.trigger_id)
        # Une page pleine ⇒ il reste peut-être des lignes : on rend un curseur. Il
        # peut mener à une page vide (la file s'arrêtait pile) — la convention des
        # autres surfaces paginées du dépôt, et la seule qui ne coûte pas une
        # requête de plus par page.
        suite = _curseur(jobs[-1]["id"]) if jobs and len(jobs) >= inp.limit else None
        if not ctx.platform_worker:
            jobs = _acces_agent.masquer_charges(ctx, jobs)
        return {"jobs": jobs,
                "total": db.count_jobs(ctx.org_id, status=inp.status,
                                       source=inp.source, fleet_id=inp.fleet_id,
                                       trigger_id=inp.trigger_id),
                "next_cursor": suite}

    # Les quatre verbes de la prise exigent le job — et le db-layer les scope au
    # CLAIMANT : un pair qui tente de conclure le job d'un autre obtient le même
    # refus qu'un job inexistant (rowcount 0 → 404), pas d'oracle.
    if inp.job_id is None:
        raise AuthzDenied(400, "missing_fields", f"{inp.op} exige `job_id`")

    if inp.op == "bind_run":
        if not inp.run_id:
            raise AuthzDenied(400, "missing_fields", "bind_run exige `run_id`")
        if not db.bind_job_run(inp.job_id, ctx.sub, inp.run_id):
            raise AuthzDenied(404, "job_not_found", "job inconnu")
        return {"ok": True}

    if inp.op == "extend":
        if not db.extend_job_lease(inp.job_id, ctx.sub,
                                   lease_seconds=max(30, min(inp.lease_seconds, 3600))):
            raise AuthzDenied(404, "job_not_found", "job inconnu")
        return {"ok": True}

    if inp.op == "reserve":
        if inp.job_id is None or inp.turn is None or inp.max_tokens is None:
            raise AuthzDenied(400, "missing_fields",
                              "reserve exige `job_id`, `turn` et `max_tokens`")
        return _reserver_un_tour(ctx, inp.job_id, inp.turn, inp.max_tokens)

    if inp.op == "meter":
        if inp.job_id is None or inp.turn is None or not inp.result:
            raise AuthzDenied(400, "missing_fields",
                              "meter exige `job_id`, `turn` et `result` (l'usage du tour)")
        return _regler_un_tour(ctx, inp.job_id, inp.turn, inp.result)

    if inp.op == "forfait":
        return _trancher_le_forfait(ctx, inp)

    if inp.op == "complete":
        if inp.ok is None:
            raise AuthzDenied(400, "missing_fields", "complete exige `ok` (true/false)")
        if inp.result is not None and len(json.dumps(inp.result)) > 4096:
            raise AuthzDenied(400, "result_too_large",
                              "result > 4 Ko — un résumé, pas un contenu")
        res = db.complete_job(inp.job_id, ctx.sub, inp.ok,
                              error=inp.error, run_id=inp.run_id, result=inp.result)
        if res is None:
            # Déjà re-claimé après bail mort, ou jamais à lui : on ne conclut pas
            # ce qui ne nous appartient plus.
            raise AuthzDenied(404, "job_not_found", "job inconnu")
        _clore_la_tentative(inp.job_id, res)
        # Ce que le worker a vu du FORFAIT (OTO-130) se porte sur la connexion du
        # demandeur. ⚠️ Worker de plateforme SEULEMENT : la conclusion est ouverte
        # à qui tient la prise, et un membre qui aurait réservé le travail d'un
        # collègue pourrait sinon le mettre en attente d'un rapport inventé.
        if ctx.platform_worker:
            _abonnement.noter_rapport_du_travail(inp.job_id, inp.ok, inp.result)
        # Le run de l'appel d'abord (c'est celui que le worker vient d'exécuter),
        # sinon celui que le job connaît (`bind_run`, ou un `continue`).
        return {"ok": True, "status": res["status"],
                **_release_run_rows(inp.run_id or res.get("run_id"),
                                    None if inp.ok else inp.error)}

    # get — lecture org-scopée (diagnostic, dashboard R4)
    job = db.get_job(inp.job_id, ctx.org_id)
    if not job:
        raise AuthzDenied(404, "job_not_found", "job inconnu")
    if not ctx.platform_worker:
        job = _acces_agent.masquer_charges(ctx, [job])[0]
    return {"job": job}


CAPABILITIES += [
    Capability(
        key="runner.jobs",
        handler=_jobs,
        Input=JobsInput,
        Output=JobsOut,
        # Déclaré parce qu'il est REJOUÉ (`tests/api/test_runner_jobs_fleet_rest.py`).
        # La liste n'est pas exhaustive par construction — les autres refus de cette
        # capacité entreront avec leur rejeu, pas avant : une déclaration sans rejeu
        # promet un statut que le serveur ne rend peut-être pas.
        errors=(
            DeclaredError(400, "org_key_only_without_provider",
                          "`claim` avec `org_key_only` mais sans `provider` : un "
                          "worker sans clé propre doit nommer le dépôt qu'il consomme"),
            DeclaredError(403, "farm_engine_platform_only",
                          "`claim engine=farm` par un porteur qui n'est pas un worker de "
                          "plateforme : seul un exécutant de la ferme réserve les travaux "
                          "d'une org routée vers elle"),
            DeclaredError(400, "farm_engine_family",
                          "`claim engine=farm` sans `provider=anthropic` : la ferme ne "
                          "sert que cette famille"),
            DeclaredError(404, "fleet_not_found",
                          "`enqueue fleet_id=` désignant une automatisation qui n'est pas "
                          "celle de l'org du porteur"),
            DeclaredError(400, "forfait_report_unreadable",
                          "`op=forfait` dont le rapport de sonde (`result.abonnement`) "
                          "est absent, mal formé, ou ne dit rien : aucun verdict n'est "
                          "rendu sur un forfait que rien n'a lu"),
            DeclaredError(409, "fleet_not_serving",
                          "`enqueue fleet_id=` désignant une automatisation ni armée ni en "
                          "cours (`draft`, `stopping`, `stopped`…) : l'exécution "
                          "échapperait à `stop`"),
        ),
        # Un WORKER de plateforme (secret de machine déclaré en base, aucun
        # compte, aucune org) ou un MEMBRE d'org. Le worker ne nomme rien et ne
        # déduit rien : le backend lui commande un travail complet — org, jeton
        # délégué au nom du déclarant, clé, procédure. Trois conceptions ont
        # précédé celle-ci en deux jours, chacune faisant PORTER ou DÉDUIRE
        # quelque chose au worker ; c'est ce pli qui était faux (09/09/2026).
        authz=WORKER_OR_ORG_MEMBER,
        mcp=None,   # worker-only : la plomberie d'exécution n'a pas de face agent
        rest=RestBinding(verb="POST", path="/api/me/runner/jobs"),
        description=(
            "The runner's execution queue (worker-facing, REST only). op=enqueue "
            "(kind start|continue — a job carries REFERENCES, never a secret) / "
            "claim (atomic, org-scoped, lease — also reclaims expired leases: that "
            "IS the resume) / bind_run / extend (heartbeat) / complete (ok=false "
            "backs off, then marks `failed` VISIBLY at the attempts cap — never "
            "loops) / get / summary (per visible agent: jobs per hour and status over "
            "`days`, the total, the latest failure and the time it saved — minutes per "
            "result type, from the logged tool calls of its `done` runs: only what a tool "
            "DECLARES counts (an unannotated tool adds nothing), and runs with no logged "
            "call are listed in `time_saved_not_counted`, never estimated; default minutes "
            "are estimates each org may override — what the Agents page draws, "
            "in one call). All claim-side verbs are scoped to the claimant. An org "
            "member (API token) claims only the jobs it carries itself (`sub`); only "
            "a platform worker takes any job."
        ),
    ),
]
