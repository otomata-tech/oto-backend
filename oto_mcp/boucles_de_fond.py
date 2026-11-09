"""Les boucles de fond du serveur — et la seule instance qui a le droit d'agir sur un tiers.

Une boucle de fond draine du travail EN ATTENTE DANS LA BASE. Or prod et préprod
partagent la même base : une boucle qui tourne en préprod draine aussi le travail de
la production. Pour une boucle qui ne fait qu'écrire en base (indexer, extraire), c'est
du travail en double. Pour une boucle qui agit sur un TIERS — prélever un abonnement,
envoyer un email —, c'est un acte fait au nom de la production par un code qu'elle n'a
pas déployé, avec les clés d'un autre environnement.

Relevé le 10/09/2026 sur les processus servis : la préprod composait les six boucles,
dont le prélèvement, avec la clé Mollie de TEST. Si elle tirait une échéance live avant
la prod, sa clé échouait sur un client live, la ligne passait `failed` et la relance
repoussait le vrai prélèvement de trois jours ; au troisième échec, le client passait
en impayé. Même mécanisme pour les emails programmés : deux schedulers sur la même file
peuvent envoyer deux fois le même message (le claim laisse la ligne `pending`).

**La règle : seule la production agit sur un tiers.** Elle vise l'axe, pas une liste.
Chaque boucle DÉCLARE si elle agit sur un tiers (`tiers`, sans valeur par défaut : une
boucle déclarée sans le dire ne s'importe pas), et hors production toutes celles qui le
déclarent sont écartées. Ajouter une boucle, c'est l'ajouter ICI : `server.main` ne
compose rien d'autre, et un test y veille.

**Ambigu = refus de démarrer**, jamais une supposition (`config.est_la_production`).
`server.main` compose avant de préparer la base : un process qui ne sait pas s'il est la
production s'arrête sans avoir rien écrit.

Les interrupteurs par boucle (`OTO_*_ENABLED=0`) restent ce qu'ils étaient : ils
ÉTEIGNENT une boucle partout, ils n'en allument jamais une hors production.

Ce que cette garde ne ferme pas : DEUX processus de production à la fois (recouvrement
bleu/vert pendant la vidange). Là, c'est la réservation en base qui tient
(`db/billing_reservation.py`), pas la composition.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import config

log = logging.getLogger(__name__)

Fonction = Callable[[], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class Boucle:
    """Une boucle de fond, et ce qu'elle fait hors de la plateforme.

    `tiers` n'a pas de défaut, exprès : c'est la question que l'auteur d'une boucle doit
    trancher, et un défaut la trancherait à sa place. `tiers=True` : la boucle produit,
    à partir de travail en base, un effet chez quelqu'un d'extérieur à la plateforme
    (un débit, un message reçu), ou écrit pour un client ce qui porte l'environnement
    qui l'a produit (un lien vers son tableau de bord) — seule la prod le fait."""

    nom: str
    tiers: bool
    armee: Callable[[], bool]
    fonction: Callable[[], Fonction]

    def __post_init__(self) -> None:
        if not isinstance(self.tiers, bool):
            raise TypeError(f"boucle {self.nom!r} : `tiers` vaut True ou False, "
                            f"pas {self.tiers!r}")


def _interrupteur(var: str) -> Callable[[], bool]:
    return lambda: os.environ.get(var, "1") != "0"


def _zero_ou_un(var: str, valeur: str) -> bool:
    if valeur not in ("0", "1"):
        raise ValueError(f"{var} vaut 0 ou 1, pas {valeur!r}")
    return valeur == "1"


def _allumee_si_declaree(var: str) -> Callable[[], bool]:
    """Éteinte tant que `var` ne vaut pas `1` : une boucle qui écrit à des clients ne
    part pas d'un oubli de réglage. Toute autre valeur que `0`/`1` refuse."""
    return lambda: _zero_ou_un(var, os.environ.get(var, "0"))


def _scheduler() -> Fonction:
    from . import scheduler
    return scheduler.run_scheduler_loop


def _embed() -> Fonction:
    from . import embed_worker
    return embed_worker.run_embed_loop


def _extraction() -> Fonction:
    from . import file_extract_worker
    return file_extract_worker.run_extract_loop


def _classement() -> Fonction:
    from . import rank_backfill_worker
    return rank_backfill_worker.run_rank_backfill_loop


def _formule_backfill() -> Fonction:
    from . import formula_backfill_worker
    return formula_backfill_worker.run_formula_backfill_loop


def _transcription() -> Fonction:
    from . import transcription_worker
    return transcription_worker.run_transcription_loop


def _jev_jobs() -> Fonction:
    from . import jev_jobs_worker
    return jev_jobs_worker.run_jev_jobs_loop


def _credits_echeances() -> Fonction:
    from . import credits_echeances
    return credits_echeances.run_credits_echeances_loop


def _alerte_agents() -> Fonction:
    from . import alerte_agents_bloques
    return alerte_agents_bloques.run_alerte_agents_loop


def _medecin_agents() -> Fonction:
    from . import medecin_agents
    return medecin_agents.run_medecin_agents_loop


def _alertes_slack_agents() -> Fonction:
    from . import alertes_slack_agents
    return alertes_slack_agents.run_alertes_slack_loop


def _alertes_slack_agents_armee() -> bool:
    """Le réglage d'instance des alertes Slack d'agents (`alertes_slack_agents.actives`,
    lu par `interrupteurs.oui_non` : une valeur illisible lève)."""
    from . import alertes_slack_agents
    return alertes_slack_agents.actives()


def _runner_tick_arme() -> bool:
    from . import runner_tick
    return runner_tick.enabled()


def _runner_tick() -> Fonction:
    from . import runner_tick
    return runner_tick.run_runner_tick_loop


def _facturation_armee() -> bool:
    from . import billing
    return (billing.is_enabled()
            and os.environ.get("OTO_BILLING_RUNNER_ENABLED", "1") != "0")


def _facturation() -> Fonction:
    from . import billing_runner
    return billing_runner.run_billing_loop


def _hang_watch_armee() -> bool:
    from . import hang_watch
    return hang_watch.enabled()


def _hang_watch() -> Fonction:
    from . import hang_watch
    return hang_watch.run_heartbeat_loop


BOUCLES: tuple[Boucle, ...] = (
    # Envoie les emails programmés (Resend, Scaleway TEM, relais) à leurs destinataires.
    Boucle(nom="scheduler", tiers=True,
           armee=_interrupteur("OTO_SCHEDULER_ENABLED"), fonction=_scheduler),
    # Indexation sémantique (lot 3) : draine l'outbox `embed_dirty`. Appelle Mistral, un
    # FOURNISSEUR : aucun effet chez un client. En double, elle coûte des jetons, elle ne
    # touche personne. No-op sans MISTRAL_API_KEY (la recherche reste lexicale).
    Boucle(nom="embed_worker", tiers=False,
           armee=_interrupteur("OTO_EMBED_WORKER_ENABLED"), fonction=_embed),
    # Extraction du texte des fichiers déposés (#298), locale. Séparée de l'indexation
    # parce que `run_embed_loop` sort d'emblée sans MISTRAL_API_KEY, alors que
    # l'extraction ne dépend d'aucun service tiers : les deux pannes restent disjointes.
    Boucle(nom="file_extract_worker", tiers=False,
           armee=_interrupteur("OTO_FILE_EXTRACT_WORKER_ENABLED"), fonction=_extraction),
    # Vecteurs de classement (#318) : remplit par tranches puis réconcilie. Hors du boot
    # par nécessité (la variante au boot tenait `datastore_rows` 7,55 s sous verrou).
    Boucle(nom="rank_backfill", tiers=False,
           armee=_interrupteur("OTO_RANK_BACKFILL_ENABLED"), fonction=_classement),
    # Colonnes formule (oto-backend#1008 v2) : draine l'outbox `formula_dirty`, posée
    # par `set_schema` au lieu d'un recalcul synchrone (mesuré au-delà du délai
    # client MCP sur un tableau de 8910 lignes). Aucun effet chez un tiers.
    Boucle(nom="formula_backfill", tiers=False,
           armee=_interrupteur("OTO_FORMULA_BACKFILL_ENABLED"),
           fonction=_formule_backfill),
    # Transcription (ADR 0074, #674) : appelle Mistral, un FOURNISSEUR. `tiers=True`
    # (oto#1363) : la page de résultat fige l'URL de SON environnement
    # (`config.dashboard_url()`) — en double prod/préprod, le gagnant de la course
    # sur `claim_next_transcription_job` pouvait figer un lien manage.oto.ninja pour
    # un client de prod.
    Boucle(nom="transcription_worker", tiers=True,
           armee=_interrupteur("OTO_TRANSCRIPTION_WORKER_ENABLED"), fonction=_transcription),
    # `jev_rows(background=true)` (jev_jobs_worker.py) : DÉPENSE sur la clé Jev partagée
    # (OpenRouter) et écrit dans les tableaux. En double prod/préprod, deux processus
    # jugeraient chacun une tranche sous des codes différents — le bail en base empêche
    # le doublon, pas la divergence : seule la prod draine.
    Boucle(nom="jev_jobs_worker", tiers=True,
           armee=_interrupteur("OTO_JEV_JOBS_WORKER_ENABLED"), fonction=_jev_jobs),
    # Échéances du budget d'unités (`credits_echeances.py`) : règle au montant réservé
    # les réservations jamais réglées. N'écrit qu'en base ; deux processus sur la même
    # échéance, un seul la règle (état `open` dans l'écriture) : `tiers=False`.
    Boucle(nom="credits_echeances", tiers=False,
           armee=_interrupteur("OTO_CREDITS_ECHEANCES_ENABLED"),
           fonction=_credits_echeances),
    # Horloge des déclencheurs du runner (R3) : ENFILE un job, n'exécute rien, c'est
    # oto-runner qui agit. Deux ticks sur la même base, un seul gagne chaque échéance
    # (CAS sur `next_due`) : aucun doublon possible, d'où `tiers=False`.
    Boucle(nom="runner_tick", tiers=False,
           armee=_runner_tick_arme, fonction=_runner_tick),
    # Prévient l'admin d'une org quand un de ses agents programmés est bloqué
    # (`alerte_agents_bloques.py`, signal 1168) : ENVOIE un courriel à un client.
    Boucle(nom="alerte_agents_bloques", tiers=True,
           armee=_allumee_si_declaree("OTO_ALERTE_AGENTS_ENABLED"), fonction=_alerte_agents),
    # Soigne un agent hébergé avant que son titulaire ne le remarque
    # (`medecin_agents.py`) : RELANCE un travail mort quand la panne est la nôtre —
    # l'agent relancé agit chez des tiers. Éteinte tant qu'elle n'est pas déclarée.
    Boucle(nom="medecin_agents", tiers=True,
           armee=_allumee_si_declaree("OTO_MEDECIN_AGENTS_ENABLED"),
           fonction=_medecin_agents),
    # Prévient une org SUR SLACK qu'un de ses agents ne fonctionne plus, qu'il a été
    # réparé, qu'il refonctionne (`alertes_slack_agents.py`) : POSTE chez un client, par
    # l'app Slack de l'org. Éteinte tant qu'elle n'est pas déclarée.
    Boucle(nom="alertes_slack_agents", tiers=True, armee=_alertes_slack_agents_armee,
           fonction=_alertes_slack_agents),
    # Échéances d'abonnement, relances, réconciliation, factures (ADR 0043) : PRÉLÈVE
    # via Mollie. Armée par le drapeau billing ET son interrupteur propre.
    Boucle(nom="billing_runner", tiers=True,
           armee=_facturation_armee, fonction=_facturation),
    # Watchdog de blocage d'event loop (`hang_watch.py`, `docs/event-loop-perf.md`) :
    # ne draine rien en base, aucun effet chez un tiers — `tiers=False` au sens le
    # plus net du mot, elle tourne dans TOUS les environnements, préprod comprise,
    # exactement là où le prochain gel non identifié peut survenir. Son interrupteur
    # propre (`OTO_HANG_WATCH_ENABLED`, défaut actif) vit dans `hang_watch.py`.
    Boucle(nom="hang_watch", tiers=False,
           armee=_hang_watch_armee, fonction=_hang_watch),
)
# (L'index BOAMP/ACCO est passé au service FOD, ADR 0028 B2b : plus de boucle ici.)


def composer() -> list[Fonction]:
    """Les boucles que CE process démarre, dans l'ordre de déclaration.

    Lève `config.EnvironnementAmbigu` si une boucle tierce est armée et que le process ne
    sait pas s'il est la production. Si aucune ne l'est, la question n'est pas posée."""
    armees = [b for b in BOUCLES if b.armee()]
    tierces = [b.nom for b in armees if b.tiers]
    if tierces and not config.est_la_production():
        # Le journal nomme CE QUI A DÉCIDÉ. Il affichait l'URL publique, du temps où
        # l'environnement s'en déduisait ; depuis qu'il se déclare (`OTO_ENV`), montrer
        # l'URL enverrait l'opérateur vérifier la mauvaise variable.
        log.info("boucles de fond : %s NON démarrée(s) — cette instance déclare servir "
                 "%r (OTO_ENV), pas la production, et elles agissent sur un tiers. La "
                 "base est partagée : c'est la production qui draine son travail vers "
                 "l'extérieur.",
                 ", ".join(tierces), config.origine_du_process())
        armees = [b for b in armees if not b.tiers]
    return [b.fonction() for b in armees]
