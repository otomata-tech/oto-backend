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
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import access, db
from ._authz import ORG_MEMBER
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
            "exists, or no longer holds a role in that organisation. The job is "
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
_OPTION_WORKER = "runner_worker"


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


def _avec_cle(job: dict, depot: Optional[str], appelant: str) -> dict:
    """Le travail, augmenté de la clé de modèle de son org — à la RÉSERVATION.

    Le worker fait partie du backend et a le droit de lire les clés que les orgs
    déposent (arbitrage du 02/09) ; ce droit s'exerce ici, une fois, avec le
    travail — jamais par un accès au coffre depuis le runner. Un worker qui
    saurait interroger le coffre pourrait lire autre chose que ce travail-ci.

    ⚠️ **Mais la file n'est pas réservée aux workers** : cette capacité est
    déclarée `ORG_MEMBER`, et rien, dans le protocole, ne distingue un worker
    d'un membre — ils portent le même genre de jeton. Sans la garde ci-dessous,
    n'importe quel membre enfilait un travail puis le réservait, et recevait la
    clé de son org EN CLAIR. Un secret que le coffre ne rend à personne, et que
    nous ne pouvons pas révoquer puisqu'il appartient au client.

    La garde porte donc sur l'ACTEUR, et par `user_has_option` — jamais
    `has_option`, qui répondrait vrai dès que l'ORG porte le don ou que son plan
    inclut l'option, c'est-à-dire pour tous ses membres à la fois.
    """
    if not depot or not job.get("org_id") or job.get("delegation_refusee"):
        return job
    if not access.user_has_option(appelant, _OPTION_WORKER):
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
        if _depot_pose(job["org_id"], depot):
            logger.warning("clé de modèle `%s` REFUSÉE à %s (org %s, travail %s) : "
                           "ce compte ne porte pas `%s`",
                           depot, appelant, job["org_id"], job.get("id"),
                           _OPTION_WORKER)
        return job
    cle = _cle_de_modele(job["org_id"], depot)
    if not cle:
        return job
    # Trace de REMISE : qui, quelle org, quel dépôt, quel travail — jamais la clé.
    # Sans elle, une remise anormale ne laisse aucune trace : le seul endroit où
    # elle se verrait serait la facture de l'org.
    logger.info("clé de modèle `%s` remise à %s pour l'org %s (travail %s)",
                depot, appelant, job["org_id"], job.get("id"))
    return {**job, "model_key": cle}


_SANS_PORTEUR = (
    "ce travail ne nomme personne — il a été enfilé avant que la file retienne "
    "son demandeur (02/09/2026). Le worker n'a pas d'identité propre à lui "
    "prêter : reprogramme-le, il partira au nom de qui le demande.")


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


def _jobs(ctx: ResolvedCtx, inp: JobsInput) -> dict:
    if not ctx.org_id:
        raise AuthzDenied(400, "org_required", "la file du runner est org-scopée")

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
        res = db.enqueue_job(ctx.org_id, inp.kind, payload=inp.payload,
                             run_id=inp.run_id, max_attempts=inp.max_attempts,
                             fleet_id=inp.fleet_id, sub=ctx.sub)
        return {"id": res["id"], "status": res["status"], "due_at": str(res["due_at"]),
                "fleet_id": res.get("fleet_id"), "sub": res.get("sub")}

    if inp.op == "claim":
        bail = max(30, min(inp.lease_seconds, 3600))
        job = db.claim_next_job(ctx.org_id, ctx.sub, lease_seconds=bail)
        if job is None:
            return {"job": None}
        return {"job": _avec_cle(_delegue(job, bail, ctx.sub), inp.provider, ctx.sub)}

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
        authz=ORG_MEMBER,
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
