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
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import _cle_exigee, _modele
from .. import db, org_store, runner_consigne, runner_models
from ._authz import WORKER_OR_ORG_MEMBER
from ._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                     RestBinding, cap_limit)
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)


class JobsInput(BaseModel):
    op: Literal["enqueue", "claim", "bind_run", "complete", "extend", "get", "list"]
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
    status: Optional[str] = None
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
    result: Optional[JobResult] = None
    due_at: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None


class JobsOut(BaseModel):
    # enqueue → id/status/due_at ; claim → job (ou null, file vide) ;
    # list → jobs + total + next_cursor ;
    # complete → ok/status + run_id/rows_released/release ; les autres → ok.
    id: Optional[int] = None
    status: Optional[str] = None
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


def _release_run_rows(run_id: Optional[str]) -> dict:
    """Le job conclu ne travaille plus : ce que son run tenait encore dans le
    datastore revient dans la file (#633) — la même troisième voie que `run_finish`,
    pour l'agent qui est MORT sans l'appeler (le worker, lui, survit à l'agent et
    conclut le job). Best-effort et HORS de la clôture : le job est déjà conclu
    quand on arrive ici, et un poste de flotte lit le compte — `0` écrit, ou `null`
    avec sa raison, jamais un 0 fabriqué."""
    if not run_id:
        return {"run_id": None, "rows_released": None, "release": "no_run"}
    try:
        n = db.datastore_release_by_run(run_id)
    except Exception:  # noqa: BLE001
        logger.warning("libération des lignes du run %s à la conclusion du job "
                       "échouée (best-effort)", run_id, exc_info=True)
        return {"run_id": run_id, "rows_released": None, "release": "failed"}
    return {"run_id": run_id, "rows_released": int(n), "release": "ok"}


# Le pouvoir délégué vit un peu plus longtemps que le bail : un agent qui
# conclut à la dernière seconde doit pouvoir écrire. Trop court couperait un
# travail abouti juste avant sa conclusion — le pire moment, puisqu'il a déjà
# tout coûté.
_MARGE_JETON_S = 120


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


def _cle_de_modele(org_id: int, depot: str) -> Optional[str]:
    """La clé de modèle DÉPOSÉE PAR L'ORG, ou None si elle n'en a pas posé.

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
        return None
    try:
        return credentials_store.get_credential("org", str(org_id), depot) or None
    except Exception:
        # Un coffre qui ne rend pas la clé n'empêche pas le travail : le worker
        # retombe sur la clé de la plateforme. Mais le silence, lui, est refusé —
        # une org qui a déposé sa clé et qu'on facture sur la nôtre doit se voir.
        logger.warning("clé de modèle `%s` illisible pour l'org %s",
                       depot, org_id, exc_info=True)
        return None


# La marque qui dit « ce compte EST un de nos workers ». Un admin plateforme la
# pose sur le compte de service du runner (`oto_admin_set_option`), et sur lui
# seul.


def _depot_pose(org_id: int, depot: str) -> bool:
    """Cette org a-t-elle DÉPOSÉ cette clé — présence seule, sans déchiffrer.

    Sert à décider s'il y a quelque chose à refuser, donc quelque chose à dire.
    `has_credential` lit la présence du chiffré (`secret_enc IS NOT NULL`) : le
    secret n'est jamais touché pour écrire une ligne de journal.

    `account=""` — le mono-compte, exactement ce que la remise lit : signaler un
    refus sur un dépôt qu'on n'aurait de toute façon pas servi serait un faux.
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
        return False


def _avec_procedure(job: dict) -> dict:
    """Le travail, augmenté du TEXTE de sa procédure — à la RÉSERVATION.

    ⚠️ Pourquoi ici et pas dans le worker. Le prompt système du runner a déjà
    porté une section « Procédure » qu'il remplissait lui-même ; on l'a retirée
    parce qu'un worker qui va CHERCHER un objet d'Oto cesse d'être un client
    pur — c'est un concept du backend dans le transport (ADR 0064). Rien n'a
    changé à cela : le worker ne cherche toujours rien. Il reçoit du texte à
    poser en cadre, exactement comme il reçoit déjà une clé de modèle et un
    jeton délégué, et il ignore ce qu'est une procédure.

    ⚠️ Joint à la réservation, JAMAIS stocké dans le travail : une consigne
    métier pèse une vingtaine de milliers de caractères, et cent travaux la
    porteraient cent fois en base pour rien. Même raison que la clé et le jeton.

    Ce que ça achète, mesuré le 08/09/2026 sur une passe réelle : la consigne
    chargée par l'agent au premier tour est FACTURÉE plein tarif au deuxième —
    20 603 jetons sur les 41 204 d'un déroulé, la moitié, avec un cache à zéro
    sur ce tour-là. Servie dans le cadre, elle entre dans le préfixe stable :
    lue en cache dès le premier tour, et le tour de chargement disparaît.

    Absente ou illisible, on ne joint rien : l'agent la chargera lui-même comme
    avant. C'est une accélération, jamais une condition."""
    p = job.get("payload") or {}
    slug, org = p.get("procedure"), job.get("org_id")
    if not slug or not org:
        return job
    # ⚠️ La procédure se lit où `oto_procedure` la lit : `org_instructions`. Cette
    # jonction lisait `get_guide_db`, qui ne sert que les guides À LA DEMANDE — un
    # autre magasin. Mesuré le 12/09/2026 sur la production : `None` pour les six
    # procédures d'une chaîne d'enrichissement que `oto_procedure` rendait en
    # version 10 à 18. La jonction n'avait donc jamais rien joint à ces passes, et
    # rien ne le disait : l'agent rechargeait la consigne, et payait le tour.
    try:
        procedure = org_store.get_instruction("org", org, slug)
    except Exception:  # noqa: BLE001
        logger.warning("procédure `%s` illisible pour l'org %s — le travail part "
                       "sans, l'agent la chargera", slug, org, exc_info=True)
        return job
    if not procedure:
        logger.warning("procédure `%s` introuvable dans l'org %s — le travail part "
                       "sans, l'agent la chargera", slug, org)
        return job
    if procedure.get("archived_at"):
        # Une procédure retirée ne se sert pas en cadre : la lecture par slug ne
        # filtre pas l'archivage (#857), c'est à l'appelant de ne pas la joindre.
        logger.warning("procédure `%s` ARCHIVÉE dans l'org %s — non jointe", slug, org)
        return job
    corps = procedure.get("body_md") or ""
    if not corps:
        return job
    return {**job, "system": corps}


def _avec_cle(job: dict, depot: Optional[str], appelant: str, *,
              worker: bool) -> dict:
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
    cle = _cle_de_modele(job["org_id"], depot)
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
    logger.info("clé de modèle `%s` remise à %s pour l'org %s (travail %s)",
                depot, appelant, job["org_id"], job.get("id"))
    return {**job, "model_key": cle}


_SANS_DEPOT = (
    "ce worker ne nomme aucun fournisseur de clé : il tournerait sur la clé de son "
    "propre environnement, et cette organisation exige que ses agents tournent sur "
    "la sienne. Travail non exécuté — il doit être servi par un worker qui nomme son "
    "dépôt (OTO_RUNNER_PROVIDER / OTO_RUNNER_OPENAI_BASE côté oto-runner).")


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


_SANS_PORTEUR = (
    "ce travail ne nomme personne — il a été enfilé avant que la file retienne "
    "son demandeur (02/09/2026). Le worker n'a pas d'identité propre à lui "
    "prêter : reprogramme-le, il partira au nom de qui le demande.")


#: Dernière cause signalée par org, pour ne pas répéter le même échec à chaque
#: sondage — `{org_id: (cause, instant)}`. En mémoire de process : au pire un
#: redémarrage rejournalise une fois, ce qui est le bon défaut.
_CAMPAGNE_MUETTE: dict[int, tuple[str, float]] = {}


def _id_du_tableau_vise(f: dict) -> Optional[int]:
    """L'identifiant du tableau que ce passage vise — résolu ICI, au nom de QUI a
    déclaré la campagne, et emporté par la charge utile du travail.

    ⚠️ **Pourquoi ici et pas à l'écran.** Un passage ne stocke qu'un NOM
    (`runner_fleets.namespace`), et `resolve_datastore_ns` préfère, à nom égal, le
    tableau personnel du DEMANDEUR. Un écran qui refait cette résolution la refait donc
    avec son propre demandeur : qui ouvre le travail d'un collègue, ou détient un
    homonyme de ce que la campagne vise, se voyait peindre les lignes du SIEN sous le
    bon libellé, sans un mot (oto#160). Résolu au nom de `f["sub"]`, l'identifiant
    désigne exactement le tableau sur lequel l'agent va travailler — même sub, même
    priorité que le store qu'il utilisera — et il vaut pour tous les lecteurs.

    ⚠️ **Fail-open, comme tout ce chemin.** Un nom qui ne résout plus (tableau supprimé,
    renommé, sorti de la portée) ne doit pas empêcher une campagne de produire : on rend
    `None`, et le travail part avec son seul nom. C'est aussi ce que portent tous les
    travaux ENFILÉS AVANT ce changement — la charge utile est persistée, elle ne se
    réécrit pas. L'écran doit donc savoir vivre sans, et ne pas prétendre ouvrir un
    tableau qu'il ne sait pas désigner."""
    ns = (f.get("namespace") or "").strip()
    if not ns or not f.get("sub"):
        return None
    try:
        from .. import group_store
        org = int(f["org_id"])
        groupes = [int(g["group_id"])
                   for g in group_store.list_groups_for_user(f["sub"], org)]
        row = db.resolve_datastore_ns(ns, sub=f["sub"], org_ids=[org],
                                      group_ids=groupes)
        return int(row["id"]) if row else None
    except Exception:  # noqa: BLE001 — voir le fail-open ci-dessus
        logger.warning("campagne %s : tableau « %s » non résolu — le travail part sans "
                       "son identifiant", f.get("id"), ns, exc_info=True)
        return None


def _produire_pour_une_campagne(org_id: Optional[int], bail_s: int) -> Optional[str]:
    """Fabrique UN travail pour une campagne en cours de l'org, s'il y en a une.

    Un seul, et sans le réserver : l'appelant re-sonde juste après et le prendra
    comme n'importe quel autre. Deux workers qui produisent en même temps ne se
    gênent pas — chacun fabrique le sien, et c'est exactement le parallélisme
    voulu ; ce qui les borne est leur nombre, pas un réglage.

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
        f = db.campagne_a_servir(org_id)
        if not f or not f.get("sub"):
            return None
        message = runner_consigne.composer(f)
        db.enqueue_job(
            # L'org de la CAMPAGNE, jamais celle de l'appelant : un worker de
            # plateforme n'en a pas, et un travail sans org serait orphelin.
            f["org_id"], "start",
            payload={"procedure": f["procedure"], "tools": list(f.get("tools") or ()),
                     "project_id": f.get("project_id"), "org_id": f["org_id"],
                     "namespace": f.get("namespace"),
                     # L'IDENTIFIANT du tableau, à côté de son nom (oto#160). Le nom
                     # seul ne désigne rien de sûr : à nom égal la résolution préfère
                     # le tableau PERSONNEL du demandeur, donc un écran qui le
                     # résoudrait le résoudrait avec SON demandeur — celui qui lit.
                     "datastore_id": _id_du_tableau_vise(f),
                     "fleet": f.get("label"),
                     "max_steps": f.get("max_steps"),
                     "max_tokens": f.get("max_tokens_per_row"),
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
            fleet_id=f["id"], sub=f["sub"])
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
    job["delegated_token"] = db.create_api_token(
        porteur, label=f"runner job {job['id']}",
        ttl_seconds=bail_s + _MARGE_JETON_S, kind="delegation")
    return job


def _charge_et_modele(ctx: ResolvedCtx, inp: JobsInput) -> Optional[dict]:
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
    if inp.kind == "continue":
        charge.update(db.modele_du_run(inp.run_id, ctx.org_id))
    else:
        model = (inp.payload or {}).get("model")
        if model is not None and not isinstance(model, str):
            raise AuthzDenied(400, "invalid_model", "`payload.model` est un nom de modèle")
        _modele.famille_declaree(model)
        charge.update(runner_models.charge(model))
    return charge if (charge or inp.payload is not None) else None


#: Ce qu'un worker sait faire — et rien d'autre. Enfiler, lister, lire un
#: travail sont des gestes d'organisation : ils exigent une org, et un worker
#: n'en a pas.
_VERBES_DU_WORKER = frozenset({"claim", "bind_run", "extend", "complete"})


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
        if inp.run_id:
            # Le gate propriétaire tient CÔTÉ SERVEUR, pas dans le séquencement de
            # l'UI : enfiler un `continue` sur le run d'autrui ferait continuer son
            # fil par le worker, avec les droits du run — un trou d'autz, pas un
            # détail. Même règle et même 404 sans oracle que l'append du fil (R1).
            head = db.get_run_head(inp.run_id)
            if not head or head.get("sub") != ctx.sub:
                raise AuthzDenied(404, "run_not_found", "run inconnu")
        # ⚠️ L'APPARTENANCE, pas seulement l'existence. La FK vers `runner_fleets`
        # garantit que la flotte existe — elle ne dit rien de QUI elle est. Sans
        # cette vérification, un travail se rattacherait à la flotte d'une autre
        # org et ferait entrer son coût et son avancement dans l'état d'un passage
        # étranger : une fuite d'observabilité, et un état faux des deux côtés.
        # Même 404 sans oracle que le gate propriétaire d'un run juste au-dessus.
        if inp.fleet_id is not None and not db.get_fleet(inp.fleet_id, ctx.org_id):
            raise AuthzDenied(404, "fleet_not_found", "flotte inconnue")
        # ⚠️ L'identité vient de l'ÉTAT SERVEUR (`ctx.sub`), jamais d'un champ
        # d'entrée : un travail dont l'appelant choisirait le porteur serait une
        # usurpation en une ligne de JSON. Le paramétrage vers un autre membre —
        # prévu par la direction du 02/09 — passera par une garde
        # d'appartenance, pas par la confiance faite au corps de la requête.
        res = db.enqueue_job(ctx.org_id, inp.kind, payload=_charge_et_modele(ctx, inp),
                             run_id=inp.run_id, max_attempts=inp.max_attempts,
                             fleet_id=inp.fleet_id, sub=ctx.sub)
        return {"id": res["id"], "status": res["status"], "due_at": str(res["due_at"]),
                "fleet_id": res.get("fleet_id"), "sub": res.get("sub")}

    if inp.op == "claim":
        bail = max(30, min(inp.lease_seconds, 3600))
        job = db.claim_next_job(ctx.org_id, ctx.sub, lease_seconds=bail,
                                depot=inp.provider)
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
                                    depot=inp.provider)
            if job is None and panne:
                # « Rien à faire » et « je n'ai pas pu regarder » ne se disent
                # pas de la même façon. Les confondre a coûté des jours de
                # sondage à vide sans que personne ne le voie (07/09/2026).
                return {"job": None, "campaign_error": panne}
        if job is None:
            return {"job": None}
        return {"job": _avec_procedure(
            _avec_cle(_delegue(job, bail, ctx.sub), inp.provider, ctx.sub,
                      worker=ctx.platform_worker))}

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
        # Le run de l'appel d'abord (c'est celui que le worker vient d'exécuter),
        # sinon celui que le job connaît (`bind_run`, ou un `continue`).
        return {"ok": True, "status": res["status"],
                **_release_run_rows(inp.run_id or res.get("run_id"))}

    # get — lecture org-scopée (diagnostic, dashboard R4)
    job = db.get_job(inp.job_id, ctx.org_id)
    if not job:
        raise AuthzDenied(404, "job_not_found", "job inconnu")
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
            DeclaredError(404, "fleet_not_found",
                          "`enqueue fleet_id=` désignant une flotte qui n'est pas "
                          "celle de l'org du porteur"),
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
            "loops) / get. All claim-side verbs are scoped to the claimant."
        ),
    ),
]
