"""La file d'exécutions du runner — claim par bail, échec visible (chantier R2).

Quatre invariants, gravés ici parce qu'une réécriture distraite les casserait :

1. **Le claim est atomique et org-scopé** : `FOR UPDATE SKIP LOCKED` sur les jobs
   de l'org du worker uniquement — deux workers ne prennent jamais le même job,
   et un worker ne voit jamais les jobs d'une autre org (V1 : un worker = un
   jeton d'org ; le pool multi-org attend l'arbitrage compte-de-service).
2. **Un bail expiré se re-claime, il ne se vole pas** : le claim reprend aussi
   les jobs `claimed` dont le bail est mort — c'est LA reprise (un worker tué
   ne bloque un job que le temps du bail), et `attempts` compte chaque prise.
3. **À bout de tentatives : `failed`, VISIBLE, jamais une boucle.** Le claim
   marque d'abord les épaves (bail mort + tentatives épuisées) avant de servir —
   refuser-et-marquer, pas tourner.
4. **Seul le claimant conclut** : `complete`/`extend`/`bind_run` sont scopés
   `claimed_by = worker` (le patron de `finish_run`) — un pair ne peut ni fermer
   ni prolonger le job d'un autre.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .. import runner_models
from ._conn import _connect

logger = logging.getLogger(__name__)

# Backoff linéaire simple : un échec renvoie le job dans la file à +30 s × tentatives.
# Pas d'exponentiel en V1 — les échecs attendus (amont LLM en vrac) se lissent, et un
# job vraiment cassé atteint son plafond en minutes, pas en heures.
_BACKOFF_S = 30
#: Les familles servies par un ABONNEMENT personnel (OTO-130) — lues du catalogue,
#: jamais recopiées. `runner_models` est PUR : la base peut le lire sans remonter
#: d'une couche (c'est déjà ce que fait la capacité).
_FAMILLES_ABONNEMENT = runner_models.FAMILLES_PERSONNELLES


def _abonnement_personnel(depot: Optional[str]) -> bool:
    """Ce worker sert-il un abonnement personnel ? Les gardes de `claim_next_job`
    qui n'ont de sens que pour un forfait ne s'allument que là."""
    return bool(depot) and depot in _FAMILLES_ABONNEMENT


_LEASE_DEFAULT_S = 600  # ~3× la ligne la plus lente mesurée (180 s) — le tour d'un run

# Plafond d'une page de file. Il existait déjà — enfoui dans le LIMIT, appliqué sans
# être dit : `limit=1000` rendait 200 lignes et rien ne l'annonçait (#469). Il est
# nommé ici parce que c'est là que le SQL le fait respecter, et déclaré au contrat
# par la capacité, qui rend le total et le curseur sans lesquels une page pleine est
# indiscernable d'une file épuisée.
JOBS_PAGE_MAX = 200

# Clé RÉSERVÉE de `payload` : ce que la PLATEFORME écrit sur un travail, jamais qui
# l'enfile (`enqueue_job` la retire). `runs_detaches` y tient l'historique COMPLET
# des runs clos détachés à la réservation (`claim_next_job`), une entrée par run
# (dédupliquée sur `run_id`) : `{run_id, tentative, raison: "run_clos", a}`, où
# `tentative` est celle qui tenait le run et `a` l'instant du détachement. C'est le
# seul chemin d'un travail vers ses runs passés : `runs` ne porte aucun lien vers le
# travail, et le `bind_run` suivant écrase `run_id`. Le worker ne la lit pas.
_CHAMP_PLATEFORME = "_plateforme"

# L'entrée ajoutée à la trace — SQL évalué dans l'UPDATE de la réservation, donc sur
# les valeurs d'AVANT (`j.run_id`, `j.attempts`). Un run déjà tracé ne l'est pas deux fois.
_TRACE_RUN_CLOS = f"""
    COALESCE(j.payload, '{{}}'::jsonb) || jsonb_build_object('{_CHAMP_PLATEFORME}',
        COALESCE(j.payload->'{_CHAMP_PLATEFORME}', '{{}}'::jsonb) || jsonb_build_object(
            'runs_detaches',
            COALESCE(j.payload->'{_CHAMP_PLATEFORME}'->'runs_detaches', '[]'::jsonb)
            || CASE WHEN COALESCE(j.payload->'{_CHAMP_PLATEFORME}'->'runs_detaches',
                                  '[]'::jsonb)
                         @> jsonb_build_array(jsonb_build_object('run_id', j.run_id))
                    THEN '[]'::jsonb
                    ELSE jsonb_build_array(jsonb_build_object(
                             'run_id', j.run_id, 'tentative', j.attempts,
                             'raison', 'run_clos', 'a', NOW()))
               END))"""


#: La clé de `_plateforme` qui dit QUEL abonnement sert un travail pris : le `sub` du
#: porteur du forfait, posé à CHAQUE réservation d'un travail d'abonnement. En mode
#: personnel c'est le demandeur ; en mode pool, le membre qui a prêté le sien. Lue par
#: la sérialisation (un travail à la fois PAR ABONNEMENT, quel que soit le mode qui l'y
#: a mis), par la remise du sandbox et par le rapport de forfait.
#:
#: ⚠️ Dans la charge (`_plateforme`, que seul le serveur écrit) et non dans une colonne :
#: aucun `ALTER` sur `runner_jobs`, la table la plus sondée de la base PARTAGÉE.
_CLE_FORFAIT = "abonnement"

#: Le porteur du forfait d'un travail, en SQL. Un travail pris avant que la clé
#: n'existe tournait sur l'abonnement de son demandeur : c'est ce que dit le COALESCE,
#: pas un repli.
_FORFAIT_SQL = (f"COALESCE({{a}}.payload->'{_CHAMP_PLATEFORME}'->>'{_CLE_FORFAIT}', "
                "{a}.sub)")


def _en_vol(porteur: str, famille: str) -> str:
    """« Un travail servi par l'abonnement `porteur` de `famille` est en vol » — la
    seule écriture de la sérialisation, que la file, le pool et `_deja_en_vol` lisent."""
    return f"""EXISTS (
                SELECT 1 FROM runner_jobs vol
                 WHERE vol.status = 'claimed' AND vol.lease_until > NOW()
                   AND vol.payload->>'model_family' = {famille}
                   AND {_FORFAIT_SQL.format(a="vol")} = {porteur})"""


_CHARGE_PRISE = f"CASE WHEN clos.a IS NULL THEN j.payload ELSE {_TRACE_RUN_CLOS} END"

#: La famille du travail examiné par la réservation.
_FAMILLE_RJ = "rj.payload->>'model_family'"


def _fragments_abonnement(abonnement: bool) -> dict:
    """Ce que la réservation ajoute pour un dépôt d'ABONNEMENT (OTO-130) — et RIEN
    pour tout autre dépôt : le SQL d'un worker de clé ne nomme même pas les tables du
    pool. Ces clauses décrivent ce qu'est un forfait, pas ce qu'est une file.

    **Mode personnel** (défaut d'une org) — le travail tourne sur l'abonnement de SON
    demandeur, `rj.sub` :

    1. UN travail à la fois par ABONNEMENT. Le bac à sable est celui de la personne,
       et deux exécutions concurrentes y partagent une même session : le
       rafraîchissement du jeton se joue entre elles. Tant que ce n'est pas mesuré
       (question U4 du plan), la file sérialise — une attente est réparable, une
       session cassée déconnecte la personne. « En vol sur cet abonnement » compte
       aussi les travaux qu'il sert pour le POOL d'une org (`_en_vol`).
    2. La personne qui ne peut pas servir ATTEND, elle n'échoue pas. Forfait épuisé :
       jusqu'à son échéance (sans échéance connue, rien ne freine — on retente, le
       fournisseur tranche). Session perdue ou déconnexion voulue : jusqu'à ce
       qu'elle se reconnecte (décidé le 21/09/2026). Arrêter ces travaux un par un
       tuait l'agent pour un état RÉPARABLE ; en attente, ils repartent tout seuls à
       la reconnexion. Ce n'est pas un arriéré qui s'accumule : le tick périme les
       occurrences programmées restées en file, et un webhook porte sa fraîcheur.

    **Mode pool** (`org_model_subscription_modes`, réglé par l'org du travail) — le
    travail tourne sur l'abonnement d'un membre de CETTE org qui l'a PRÊTÉ
    (`user_model_subscription_loans`), toujours membre, servable (même règle que la
    clause 2 : `org_subscription_pool.PRETEUR_SERVABLE`) et sans travail en vol. Le
    prêteur servi le MOINS récemment d'abord (`servi_at`, NULL en tête). Aucun de
    libre : le travail ATTEND, `pending`, aucune tentative brûlée — jamais un échec.
    L'état du demandeur ne compte pas : il n'est pas celui qui paie.

    Le porteur choisi s'écrit dans la charge (`_plateforme.abonnement`) par la même
    écriture que la prise ; `_deja_en_vol` tranche ensuite la course de deux
    réservations simultanées sur le même abonnement."""
    if not abonnement:
        return {"colonnes_forfait": "", "jointure_pret": "", "clause_abonnement": "",
                "verrou_de": "", "charge_prise": _CHARGE_PRISE, "retour_preteur": ""}
    from .org_subscription_pool import PRET_VIVANT, PRETEUR_SERVABLE
    en_pool = f"""EXISTS (
                SELECT 1 FROM org_model_subscription_modes m
                 WHERE m.org_id = rj.org_id AND m.famille = {_FAMILLE_RJ}
                   AND m.mode = 'pool')"""
    return {
        "colonnes_forfait": ", pret.sub AS preteur, COALESCE(pret.sub, rj.sub) AS forfait",
        # ⚠️ LATERAL, et `FOR UPDATE OF rj` : la ligne du prêteur n'est pas verrouillée
        # (une jointure externe ne se verrouille pas), la course entre deux prises sur
        # le même prêteur se tranche au verrou consultatif de `_deja_en_vol`.
        "jointure_pret": f"""LEFT JOIN LATERAL (
                    SELECT l.sub FROM user_model_subscription_loans l
                      JOIN org_model_subscription_modes m
                        ON m.org_id = l.org_id AND m.famille = l.famille
                       AND m.mode = 'pool'
                      {PRET_VIVANT}
                     WHERE l.org_id = rj.org_id AND l.famille = {_FAMILLE_RJ}
                       AND {PRETEUR_SERVABLE}
                       AND NOT {_en_vol("l.sub", "l.famille")}
                     ORDER BY l.servi_at NULLS FIRST, l.created_at, l.sub
                     LIMIT 1) pret ON TRUE""",
        "clause_abonnement": f"""AND CASE WHEN {en_pool}
                        THEN pret.sub IS NOT NULL
                        ELSE NOT {_en_vol("rj.sub", _FAMILLE_RJ)}
                             AND NOT EXISTS (
                                 SELECT 1 FROM user_model_subscriptions ab
                                  WHERE ab.sub = rj.sub
                                    AND ab.famille = {_FAMILLE_RJ}
                                    AND (ab.statut IN ('needs_login', 'disconnected')
                                         OR (ab.statut = 'paused_limit'
                                             AND ab.limit_reset_at > NOW())))
                   END""",
        "verrou_de": "OF rj ",
        "charge_prise": f"""(SELECT jsonb_set(
                        c.charge, '{{{_CHAMP_PLATEFORME}}}',
                        COALESCE(c.charge->'{_CHAMP_PLATEFORME}', '{{}}'::jsonb)
                        || jsonb_build_object('{_CLE_FORFAIT}', pris.forfait))
                     FROM (SELECT COALESCE({_CHARGE_PRISE}, '{{}}'::jsonb) AS charge) c)""",
        # `fleet_id` : la remise refuse une flotte hors pool (`_avec_abonnement`) — un
        # passage armé en pool, dont l'org est repassée en personnel, ferait sinon
        # payer le forfait de son créateur pour le travail de tous.
        "retour_preteur": ", j.fleet_id, pris.preteur AS _preteur",
    }


def enqueue_job(org_id: int, kind: str, payload: Optional[dict] = None,
                run_id: Optional[str] = None, max_attempts: int = 3,
                fleet_id: Optional[int] = None,
                sub: Optional[str] = None,
                delai_s: Optional[int] = None,
                perime_apres_s: Optional[int] = None,
                conn=None,
                seulement_si_servable: bool = False) -> Optional[dict]:
    """Enfile un travail, éventuellement rattaché à une FLOTTE.

    ⚠️ `sub` = **l'identité que l'agent portera en exécutant ce travail**, pas
    une simple trace d'audit. C'est le préalable du worker mutualisé : tant que
    l'identité vient du jeton présenté par le worker, il faut un worker par
    organisation. Absent = créateur inconnu (travaux d'avant le 02/09) ; on ne
    lui en invente pas un.

    ⚠️ `fleet_id` est ce qui rend un passage lisible d'un bout à l'autre : sans
    lui, `runner.fleets op=state` agrège sur un ensemble vide et répond
    `no_jobs_attached` pour toute flotte, toujours. La colonne existait depuis R4
    sans le moindre écrivain servi — une lecture complète à qui il manquait de
    quoi lire (#791).

    ⚠️ **L'APPARTENANCE de la flotte se vérifie AVANT**, chez l'appelant : la FK
    garantit que la flotte EXISTE, pas qu'elle soit celle de cette org. Rattacher
    un travail à la flotte d'autrui ferait entrer son coût et son avancement dans
    l'état d'un passage étranger. Son ÉTAT aussi, dans la même transaction
    (`conn`) : `runner.jobs op=enqueue` refuse une flotte hors
    `STATUTS_QUI_SERVENT` sous `verrouiller_la_flotte` (oto-backend#996).

    ⚠️ `_plateforme` (`_CHAMP_PLATEFORME`) est RETIRÉ de la charge : le serveur seul
    l'écrit, à la réservation. Retiré plutôt que refusé, comme la capacité retire
    `model_family` : une `ValueError` d'ici sortirait en 500, pas en refus nommé.

    ⚠️ **`_perime_apres_s` est retiré de la charge de l'appelant pour la MÊME
    raison, et son oubli a coûté un incident** (15/09/2026) : la réservation caste
    ce champ (`(payload->>'_perime_apres_s')::int`) à CHAQUE `claim_next_job`, y
    compris pour un worker de PLATEFORME (`org_id IS NULL`, donc sans filtre
    d'org). Une charge fournie par un seul appelant portant une valeur non-entière
    (ou hors bornes `int`) y faisait lever une exception PostgreSQL — arrêtant la
    réservation de TOUTE la flotte, pas seulement de l'org fautive. Le champ ne
    doit avoir qu'un seul écrivain : ce paramètre, jamais la charge reçue.

    `delai_s` (12/09/2026) : le travail ne devient réservable que dans N secondes.
    C'est ce qui rend le LISSAGE d'une rafale de webhooks possible sans rien
    perdre — au-delà du débit déclaré, la livraison est acceptée et son travail
    part plus tard. Le mécanisme existait déjà dans la table (`due_at`, que le
    claim filtre) ; il n'avait simplement aucun écrivain autre que « maintenant ».

    `perime_apres_s` : au-delà, ce travail ne doit plus partir. Posé dans la
    CHARGE (`_perime_apres`) plutôt que dans une colonne, parce que la réservation
    est le seul endroit qui puisse l'appliquer sans qu'une boucle de fond doive
    exister — et parce qu'un travail sans lui reste identique, octet pour octet.
    ⚠️ Un travail retardé QUI PÉRIME est le seul garde-fou contre un lissage qui
    deviendrait un arriéré : un événement d'hier joué demain rend un résultat
    faux, pas un résultat tardif (la leçon de #814).

    `conn` : la connexion de l'appelant, pour que l'enfilage partage SA
    transaction. La route des webhooks écrit la livraison et le travail ensemble
    ou pas du tout.

    `seulement_si_servable` (#907, oto#245) : n'enfiler que si la campagne
    `fleet_id` est ENCORE servable (`runner_fleets._ELIGIBLE` : armée, sans travail
    en attente, sous `max_rows`, moins de `workers` travaux en cours) — et rendre
    `None` sinon. La revérification et l'INSERT partagent la transaction et le
    verrou de campagne : c'est ce qui fait de `workers` une borne TENUE et non lue.
    ⚠️ Le verrou se prend dans une instruction À PART, avant l'INSERT : en
    READ COMMITTED l'instantané d'une instruction date de son DÉBUT, donc un verrou
    pris dans le `WHERE` de l'INSERT arriverait après l'instantané et ne verrait
    pas le travail qu'un sondage concurrent vient de valider.
    """
    if payload is not None:
        payload = {k: v for k, v in payload.items()
                  if k not in (_CHAMP_PLATEFORME, "_perime_apres_s")}
    charge = dict(payload) if payload is not None else None
    if perime_apres_s:
        charge = charge or {}
        charge["_perime_apres_s"] = int(perime_apres_s)

    def _poser(c):
        return c.execute(
            """
            INSERT INTO runner_jobs (org_id, kind, payload, run_id, max_attempts,
                                     fleet_id, sub, due_at)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s,
                    NOW() + make_interval(secs => %s))
            RETURNING id, status, due_at, fleet_id, sub
            """,
            (org_id, kind,
             json.dumps(charge, ensure_ascii=False) if charge is not None else None,
             run_id, max(1, int(max_attempts)), fleet_id, sub,
             max(0, int(delai_s or 0))),
        ).fetchone()

    def _poser_si_servable(c):
        from .runner_fleets import _ELIGIBLE, _VERROU_CAMPAGNE
        c.execute("SET LOCAL lock_timeout = '200ms'")
        # Non bloquant, comme `campagne_a_servir` : un sondage qui arrive pendant
        # qu'un autre produit repart les mains vides et re-sondera.
        verrou = c.execute("SELECT pg_try_advisory_xact_lock(%s, %s::int) AS tenu",
                           (_VERROU_CAMPAGNE, fleet_id)).fetchone()
        if not verrou["tenu"]:
            return None
        return c.execute(
            f"""
            INSERT INTO runner_jobs (org_id, kind, payload, run_id, max_attempts,
                                     fleet_id, sub, due_at)
            SELECT %s, %s, %s::jsonb, %s, %s, f.id, %s,
                   NOW() + make_interval(secs => %s)
              FROM runner_fleets f
             WHERE f.id = %s AND {_ELIGIBLE}
            RETURNING id, status, due_at, fleet_id, sub
            """,
            (org_id, kind,
             json.dumps(charge, ensure_ascii=False) if charge is not None else None,
             run_id, max(1, int(max_attempts)), sub,
             max(0, int(delai_s or 0)), fleet_id),
        ).fetchone()

    if seulement_si_servable:
        if fleet_id is None:
            raise ValueError("seulement_si_servable exige fleet_id")
        poser = _poser_si_servable
    else:
        poser = _poser
    if conn is not None:
        row = poser(conn)
    else:
        with _connect() as c:
            row = poser(c)
    return dict(row) if row is not None else None


_RAISON_CYCLE = ("occurrence non prise dans son cycle : le déclencheur a enfilé "
                 "la suivante. Aucun agent ne dessert cette organisation.")


def perimer_travaux_du_declencheur(trigger_id: int, org_id: int,
                                   raison: str = _RAISON_CYCLE) -> int:
    """Périme les travaux `pending` d'un déclencheur que personne n'a pris.

    Appelée quand le tick enfile l'occurrence SUIVANTE : ce qui restait en
    attente n'a pas été pris **dans son cycle**, et ne le sera plus utilement.

    ⚠️ **La définition du « trop tard » vient du cron lui-même**, pas d'un délai
    choisi. Un délai fixe serait faux des deux côtés à la fois : trop court pour
    une veille mensuelle, absurdement long pour une veille horaire. Ici, une
    occurrence périme exactement quand la suivante arrive — la règle est la même
    pour toutes les cadences et il n'y a aucun réglage à tenir à jour.

    ⚠️ **Et elle ne SUPPRIME rien.** Un travail qui disparaît remplacerait un
    trou silencieux par un pire : il effacerait la preuve du premier. 41 travaux
    empilés depuis treize jours ont été le seul indice qu'une automatisation ne
    tournait pas (02/09) ; purgés à mesure, personne n'aurait jamais rien vu.
    L'état `expired` est ce qui rend la perte COMPTABLE.

    ⚠️ `expired` n'est pas `failed` : ce travail n'a jamais tourné. « Échoué »
    envoie chercher une erreur d'exécution qui n'existe pas, quand le fait est
    « personne n'est venu le prendre » — et les deux ne se réparent pas pareil.

    ⚠️ Couvre aussi les travaux RETENUS (`held`, 13/09/2026) : on vide la file
    d'un agent déclenché **pendant qu'il est en pause**, c'est même le moment le
    plus naturel pour le faire. Les oublier laisserait le seul geste de purge sans
    effet exactement là où on s'en sert.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs
               SET status = 'expired', finished_at = NOW(),
                   last_error = %s
             WHERE org_id = %s AND status IN ('pending', 'held')
               AND payload->>'trigger_id' = %s
            """,
            (raison, org_id, str(trigger_id)),
        )
        return cur.rowcount or 0


def file_du_declencheur(trigger_id: int, org_id: int) -> dict:
    """Ce qui ATTEND maintenant pour ce déclencheur : `{pending, held}`.

    ⚠️ **Exactement le prédicat de `perimer_travaux_du_declencheur`**, et c'est la
    raison d'être de ce compte : il dit ce que « vider la file » viderait. Un écran
    qui pose ce bouton sous le journal des livraisons, sans ce nombre, laisse lire
    le journal comme la file — deux livraisons terminées depuis des heures y
    passaient pour deux événements en attente (16/09/2026).

    `held` est séparé de `pending` parce qu'ils ne disent pas la même chose :
    `pending` part dès qu'un worker passe, `held` attend qu'on rallume l'agent.
    `0` est un vrai zéro, jamais une absence de mesure.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'pending')::int AS pending,
                   COUNT(*) FILTER (WHERE status = 'held')::int AS held
              FROM runner_jobs
             WHERE org_id = %s AND status IN ('pending', 'held')
               AND payload->>'trigger_id' = %s
            """,
            (org_id, str(trigger_id)),
        ).fetchone()
    d = dict(row) if row else {}
    return {"pending": d.get("pending") or 0, "held": d.get("held") or 0}


def comptage_perime(org_id: int, trigger_id: int) -> dict:
    """Ce qu'un déclencheur a PERDU : combien d'occurrences, et depuis quand.

    ⚠️ Dérivé de la file, jamais recopié sur le déclencheur : un compteur tenu à
    part diverge de ce qu'il compte, et c'est alors le compteur qu'on croit.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)::int AS expired_count,
                   MIN(due_at)   AS expired_since,
                   MAX(due_at)   AS expired_last
              FROM runner_jobs
             WHERE org_id = %s AND status = 'expired'
               AND payload->>'trigger_id' = %s
            """,
            (org_id, str(trigger_id)),
        ).fetchone()
    d = dict(row) if row else {}
    return {"expired_count": d.get("expired_count") or 0,
            "expired_since": d.get("expired_since"),
            "expired_last": d.get("expired_last")}


#: Granularité de la marque de présence d'un worker de PLATEFORME — bien en
#: dessous d'`ARME_FENETRE_S` (15 min) : un lecteur de `runner_arme`/`families`
#: (fenêtre de 15 min) ne voit jamais la différence entre « vu il y a 3 s » et
#: « vu il y a 28 s ». Une écriture évitée sous ce seuil ne prend aucun verrou.
#: Seul point d'écriture de `runner_platform_workers.last_seen_at` (17/09/2026) —
#: `verify_worker_secret` (`runner_workers.py`) est une lecture pure.
_PRESENCE_GRANULARITE_S = 30

#: Séparateur de la PORTÉE d'une présence par famille (`anthropic@org:226`) : un
#: worker filtré par `org_ids` ne sert sa famille qu'à ces orgs-là.
_PORTEE_ORG = "@org:"

#: ── ROUTAGE D'UNE ORG VERS LA FERME (29/09/2026) ─────────────────────────────
#: L'OPTION d'org (`oto_admin_set_option entity_type=org option=claude_farm`) qui
#: réserve les travaux de la famille `FAMILLE_FERME` de cette org aux seuls workers
#: de la ferme Claude Code (`engine=farm` au claim). Sans elle, un worker de la
#: boucle de la même famille les prend aussi — le premier qui réserve gagne, et un
#: moteur qu'on ne voulait pas pour cette org sert une part de ses travaux.
#: ⚠️ Jamais de retombée sur la boucle : faute de worker ferme vivant, le travail
#: ATTEND, et `runner_arme` ne déclare pas la famille servie pour cette org
#: (`model_not_served` à la pose, avec la raison).
OPTION_FERME = "claude_farm"
#: La famille que la ferme sert — la seule qu'une org routée lui réserve.
FAMILLE_FERME = "anthropic"
#: La marque d'une présence de FERME dans `runner_platform_depots`
#: (`anthropic#ferme`, `anthropic#ferme@org:178`) — sans DDL : `depot` est un TEXT.
_MARQUE_FERME = "#ferme"


def _org_routee_ferme(alias: str) -> str:
    """Le prédicat SQL « l'org de `alias` est routée vers la ferme » : un don d'option
    VIVANT, même lecture que `has_option_comp` (l'échéance mord en SQL)."""
    return (f"EXISTS (SELECT 1 FROM option_comps oc WHERE oc.entity_type = 'org' "
            f"AND oc.entity_id = {alias}.org_id::text AND oc.option = '{OPTION_FERME}' "
            f"AND (oc.expires_at IS NULL OR oc.expires_at > NOW()))")


def _touch_platform_worker_presence(worker_sub: str, depot: Optional[str],
                                    org_ids: Optional[list] = None,
                                    ferme: bool = False) -> None:
    """Marque la présence d'un worker de PLATEFORME — SA PROPRE connexion,
    courte, committée avant que `claim_next_job` n'ouvre sa transaction de
    réservation.

    ⚠️ **C'est ICI, et seulement ici, que `runner_platform_workers` s'écrit**
    (17/09/2026, revue oto cd) — PAS dans `verify_worker_secret` (lecture pure
    depuis ce lot). Deux raisons de choisir ce point plutôt que l'authentification :
    un worker à jeton d'ORG (`OTO_RUNNER_ARMED=1`, oto-runner) sonde `claim_next_job`
    sans jamais passer par `verify_worker_secret`, réservé aux workers de
    PLATEFORME (préfixe `otow_`) — une écriture posée côté auth resterait
    invisible pour ces workers-là, et `runner_arme`/`no_runner_armed` les
    verrait toujours absents. Et l'`INSERT … ON CONFLICT` ci-dessous CRÉE la
    ligne si elle n'existe pas encore (`ON CONFLICT DO UPDATE`, avec la fenêtre
    en `WHERE`), quand un `UPDATE` seul ne réagirait jamais à une ligne absente
    — le cas exact d'un worker qui sonde avant d'avoir jamais été vu.
    `runner_platform_depots`, elle, est keyée `(worker_sub, depot)` : c'est une
    ligne PAR dépôt.

    ⚠️ **Un worker FILTRÉ (`org_ids`, 25/09/2026) note sa famille AVEC SA PORTÉE**
    (`<famille>@org:<id>`, une ligne par org servie), jamais nue : noté nu, un worker
    d'essai seul sur une famille la ferait lire « servie » par TOUTES les orgs, qui
    poseraient des agents qu'il ne prendra jamais — exactement ce que
    `no_runner_armed` existe pour empêcher. `runner_arme(org)` ne compte une famille
    portée que pour les orgs qu'elle nomme. Sans DDL : `depot` est un TEXT.

    ⚠️ **Un worker de FERME (`ferme`, 29/09/2026) marque sa présence** (`#ferme`
    entre la famille et la portée) : pour une org routée vers la ferme
    (`OPTION_FERME`), seule une présence marquée rend sa famille servie."""
    with _connect() as conn:
        conn.execute(
            f"""
            INSERT INTO runner_platform_workers (worker_sub, last_seen_at)
                 VALUES (%s, NOW())
            ON CONFLICT (worker_sub) DO UPDATE
               SET last_seen_at = NOW()
             WHERE runner_platform_workers.last_seen_at
                   < NOW() - interval '{_PRESENCE_GRANULARITE_S} seconds'
            """,
            (worker_sub,),
        )
        # La présence PAR FAMILLE — ce que `runner_arme` rend en `families`.
        # Seules les familles du catalogue se notent : `provider` est une
        # chaîne libre, et un dépôt que rien ne route n'a rien à promettre.
        from ..runner_models import FAMILLES
        if depot in FAMILLES:
            base = f"{depot}{_MARQUE_FERME}" if ferme else depot
            depots = ([f"{base}{_PORTEE_ORG}{int(o)}" for o in org_ids]
                      if org_ids else [base])
            for cle in depots:
                conn.execute(
                    f"""
                    INSERT INTO runner_platform_depots (worker_sub, depot, last_seen_at)
                         VALUES (%s, %s, NOW())
                    ON CONFLICT (worker_sub, depot) DO UPDATE
                       SET last_seen_at = NOW()
                     WHERE runner_platform_depots.last_seen_at
                           < NOW() - interval '{_PRESENCE_GRANULARITE_S} seconds'
                    """,
                    (worker_sub, cle),
                )


def claim_next_job(org_id: Optional[int], worker_sub: str,
                   lease_seconds: int = _LEASE_DEFAULT_S,
                   depot: Optional[str] = None,
                   famille_seule: bool = False,
                   org_ids: Optional[list] = None,
                   ferme: bool = False) -> Optional[dict]:
    """Le prochain job, bail posé — ou None (file vide).

    ⚠️ `depot` = le dépôt de clé que le worker nomme, c'est-à-dire la FAMILLE de
    modèles qu'il sait servir (`runner_models`). Il ne réserve que les travaux de
    cette famille ET ceux qui n'en portent aucune — un travail sans famille est
    servi par n'importe qui, sur son propre modèle, comme avant le 12/09/2026.
    Sans dépôt, il ne prend QUE les travaux sans famille : un worker qui ne dit
    pas ce qu'il sert ne reçoit jamais un modèle qu'il ne saurait pas appeler.

    ⚠️ `famille_seule` (13/09/2026) : le worker ne prend QUE les travaux de sa
    famille — jamais ceux qui n'en portent aucune. C'est ce que demande un worker
    qui ne tient AUCUNE clé de modèle à lui (`org_key_only` côté capacité) : un
    travail sans famille est celui d'un agent posé sans modèle, que les workers
    existants servent sur LEUR modèle. Le prendre lui ferait changer de
    fournisseur en silence — et, faute de clé de plateforme, échouer. Sans ce
    filtre, ouvrir un pool Anthropic « clés clients seules » aurait volé et cassé
    les agents historiques des organisations qui n'ont pas déposé de clé.

    Marque d'abord `failed` les épaves (bail mort + tentatives épuisées) : elles
    deviennent VISIBLES au lieu d'être re-servies pour rien.

    ⚠️ `org_id=None` = **worker de PLATEFORME** : il sert toutes les
    organisations, et prend simplement le travail le plus ancien. Ce n'est pas
    une garde retirée — un worker n'a jamais eu de droit propre : ce qu'il
    reçoit avec le travail est un jeton délégué au nom du DEMANDEUR, borné au
    bail. Le filtre par org venait de ce que la capacité était déclarée pour un
    membre d'organisation, ce qui imposait un worker par client.

    ⚠️ DETTE, nommée ici parce que c'est ici qu'elle mordra : l'ordre est
    `due_at`, donc FIFO GLOBAL. Une organisation qui enfile deux mille travaux
    fait attendre toutes les autres — le partage est équitable dans le TEMPS,
    pas entre clients. Un tourniquet par organisation est le geste suivant ; il
    n'est pas fait.

    `org_ids` (25/09/2026) : le worker ne réserve QUE les travaux de ces orgs — pour
    essayer un moteur sur une organisation avant de le donner au parc. `None` = toutes,
    comme avant. Il RESTREINT, il n'élargit jamais : un appelant scopé à son org
    (`org_id`) ne voit que l'intersection. Seule la PRISE est filtrée ; les épaves et les
    périmés de toutes les orgs se constatent toujours au sondage, comme avant.

    `ferme` (29/09/2026) : le worker est un exécutant de la FERME Claude Code. Un
    travail de la famille `FAMILLE_FERME` d'une org routée vers la ferme
    (`OPTION_FERME`) n'est réservé QUE par lui : tout autre worker ne le voit plus,
    et il attend plutôt que de retomber sur la boucle."""
    if org_id is None:
        # Le SONDAGE vaut présence, HORS de la transaction de réservation
        # ci-dessous (oto-backend, lot perf 17/09/2026, mesuré par oto cd :
        # `runner_platform_workers` ne porte QU'UNE ligne — les 12 unités
        # `oto-runner@N` partagent un seul secret — et un upsert de présence
        # posé ICI, dans la même transaction que le `FOR UPDATE SKIP LOCKED`,
        # tenait le verrou de cette ligne pendant TOUTE la réservation : les
        # 12 workers passaient un par un. Sa propre connexion, courte,
        # committée avant que la réservation ne commence.
        _touch_platform_worker_presence(worker_sub, depot, org_ids, ferme)
    with _connect() as conn:
        if org_id is not None:
            conn.execute(
                """
                INSERT INTO runner_workers (org_id, worker_sub, last_seen_at)
                     VALUES (%s, %s, NOW())
                ON CONFLICT (org_id, worker_sub)
                  DO UPDATE SET last_seen_at = NOW()
                """,
                (org_id, worker_sub),
            )
        conn.execute(
            """
            UPDATE runner_jobs
               SET status = 'failed', finished_at = NOW(),
                   last_error = COALESCE(last_error, '') ||
                                ' [bail expiré, tentatives épuisées]'
             WHERE (%s::bigint IS NULL OR org_id = %s) AND status = 'claimed'
               AND lease_until < NOW() AND attempts >= max_attempts
            """,
            (org_id, org_id),
        )
        # ⚠️ Ce qui a trop attendu PÉRIME, avant d'être servi (12/09/2026). Un
        # travail déclenché par un webhook porte sa fraîcheur dans sa charge
        # (`_perime_apres_s`, posé par `enqueue_job`) : au-delà, il ne part plus.
        #
        # Sans ça, un lissage devient un ARRIÉRÉ — une rafale retardée se
        # déverserait le lendemain, et un agent traiterait un événement d'hier
        # comme s'il venait d'arriver. C'est exactement la faute que les
        # occurrences programmées ont payée (#814) : « une veille quotidienne
        # exécutée treize jours plus tard ne rend pas un résultat en retard, elle
        # rend un résultat FAUX ».
        #
        # `expired` et non `failed` : ce travail n'a jamais tourné. Et ici plutôt
        # que dans une boucle de fond, pour la même raison que les épaves
        # ci-dessus — le sondage est le seul rendez-vous garanti.
        conn.execute(
            """
            UPDATE runner_jobs
               SET status = 'expired', finished_at = NOW(),
                   last_error = 'livraison périmée : le travail a attendu plus '
                                'longtemps que la fraîcheur déclarée par son '
                                'déclencheur. Un événement traité trop tard rend '
                                'un résultat faux, pas un résultat tardif.'
             WHERE (%s::bigint IS NULL OR org_id = %s) AND status = 'pending'
               AND payload ? '_perime_apres_s'
               AND created_at + make_interval(
                       secs => (payload->>'_perime_apres_s')::int) < NOW()
            """,
            (org_id, org_id),
        )
        # ⚠️ Un `start` dont le run lié est CLOS se sert SANS run : le worker lit
        # `run_id` absent comme « ouvre un run neuf », et reprendrait sinon un fil
        # clos dont l'historique tient des lignes déjà libérées. La clôture se lit
        # du FAIT `run_finish` (`_run_closure`, la vérité de `run_closed_at`) : il
        # s'inscrit en tâche de fond APRÈS la réponse, donc pas lisible au
        # `complete` qui suit — ici, backoff ou bail mort lui ont laissé le temps.
        # Sans fait inscrit, rien n'est détaché. Jamais un `continue` (il EST la
        # reprise d'un fil), jamais un bail repris sur un run ouvert. La trace
        # (`_CHAMP_PLATEFORME`) s'écrit dans la MÊME écriture que le détachement.
        from .usage import _run_closure
        orgs = [int(o) for o in org_ids] if org_ids else None
        abonnement = _abonnement_personnel(depot)
        if abonnement:
            # Point de sauvegarde, et non un rollback : la connexion peut être
            # PARTAGÉE avec un appelant (`_emprunt_partage`), dont une annulation
            # entière déferait aussi les écritures. Seule la prise se défait.
            conn.execute("SAVEPOINT prise_abonnement")
        frag = _fragments_abonnement(abonnement)
        row = conn.execute(
            f"""
            WITH pris AS (
                SELECT rj.id, rj.kind, rj.run_id{frag['colonnes_forfait']} FROM runner_jobs rj
                {frag['jointure_pret']}
                 WHERE (%s::bigint IS NULL OR org_id = %s) AND due_at <= NOW()
                   AND (%s::bigint[] IS NULL OR org_id = ANY(%s::bigint[]))
                   -- Org SUSPENDUE (`org_suspension`) : ses travaux restent en file,
                   -- intacts, et ne sont pas réservés. Ils repartent à la levée.
                   AND NOT EXISTS (SELECT 1 FROM orgs o_susp WHERE o_susp.id = rj.org_id
                                    AND o_susp.suspended_at IS NOT NULL)
                   -- ⚠️ La forme `status IN (...) AND (status = 'pending' OR ...)`
                   -- n'est pas cosmétique : le `OR` nu d'avant (17/09/2026, cf.
                   -- oto-backend#deadlock) empêchait le planificateur de se limiter
                   -- à l'index partiel `idx_runner_jobs_live` — il ne peut se
                   -- restreindre à un index PARTIEL que si le WHERE IMPLIQUE son
                   -- prédicat, et une disjonction sur DEUX statuts écrite sans le
                   -- dire explicitement ne le prouve pas au planificateur.
                   AND status IN ('pending', 'claimed')
                   AND (status = 'pending' OR lease_until < NOW())
                   AND attempts < max_attempts
                   -- ⚠️ `''` et jamais NULL pour « aucun dépôt » : `= ''` rend
                   -- FAUX, `= NULL` rend INCONNU. Même tri dans cette forme,
                   -- mais la première réécriture qui NIE la clause (`NOT …`)
                   -- ferait de l'inconnu une exclusion muette.
                   -- ⚠️ La négation porte sur le BOOLÉEN `famille_seule`, jamais
                   -- sur la clause de famille : un worker « famille seule » perd
                   -- les travaux sans famille, et rien d'autre ne bouge. (Pas de
                   -- marqueur de paramètre dans ce commentaire : psycopg les compte
                   -- aussi dans les commentaires SQL.)
                   AND (payload->>'model_family' = %s
                        OR (payload->>'model_family' IS NULL AND NOT %s))
                   -- Une org routée vers la ferme : sa famille ferme n'est prise
                   -- que par un worker de ferme (le booléen en tête).
                   AND (%s OR rj.payload->>'model_family' IS DISTINCT FROM '{FAMILLE_FERME}'
                        OR NOT {_org_routee_ferme("rj")})
                   {frag['clause_abonnement']}
                 ORDER BY due_at
                   FOR UPDATE {frag['verrou_de']}SKIP LOCKED
                 LIMIT 1
            ), clos AS (
                -- Le DERNIER `run_start` du run, et sa clôture : même lecture que
                -- `run_closed_at` (une clôture antérieure à l'ouverture ne compte pas).
                SELECT f.created_at AS a
                  FROM pris
                  JOIN tool_calls s ON s.tool = 'run_start' AND s.run_id = pris.run_id
                  {_run_closure("s")}
                 WHERE pris.kind = 'start'
                 ORDER BY s.created_at DESC
                 LIMIT 1
            )
            UPDATE runner_jobs j
               SET status = 'claimed', claimed_by = %s, attempts = j.attempts + 1,
                   lease_until = NOW() + make_interval(secs => %s),
                   run_id  = CASE WHEN clos.a IS NULL THEN j.run_id END,
                   payload = {frag['charge_prise']}
              FROM pris LEFT JOIN clos ON TRUE
             WHERE j.id = pris.id
            RETURNING j.id, j.kind, j.run_id, j.payload, j.attempts, j.max_attempts,
                      j.lease_until, j.sub, j.org_id{frag['retour_preteur']}
            """,
            (org_id, org_id, orgs, orgs, depot or "", bool(famille_seule),
             bool(ferme), worker_sub, int(lease_seconds)),
        ).fetchone()
        row = dict(row) if row else None
        if abonnement:
            preteur = row.pop("_preteur", None) if row else None
            if row and _deja_en_vol(conn, row):
                # Un autre worker a pris, AU MÊME INSTANT, un autre travail servi par
                # le MÊME abonnement (le même demandeur, ou le même prêteur du pool).
                # Cette prise-ci se défait : le travail retourne `pending`, sa
                # tentative n'est pas comptée, et il repartira au sondage suivant —
                # sur un autre prêteur s'il y en a un de libre.
                conn.execute("ROLLBACK TO SAVEPOINT prise_abonnement")
                row = None
            elif row and preteur:
                # Le tourniquet du pool : ce prêteur passe en DERNIER, dans toutes
                # les orgs auxquelles il prête (un forfait, une file d'attente).
                conn.execute(
                    "UPDATE user_model_subscription_loans SET servi_at = NOW() "
                    "WHERE sub = %s AND famille = %s",
                    (preteur, (row.get("payload") or {}).get("model_family")))
            conn.execute("RELEASE SAVEPOINT prise_abonnement")
    return row


#: ────────────────────────────────────────────────────────────────────────────
#: REPLI PLAFOND → clé API (OTO-130, 27/09/2026)
#:
#: Un travail d'ABONNEMENT dont le SEUL obstacle est un PLAFOND de consommation
#: (`paused_limit`, échéance FUTURE) — jamais `needs_login`/`disconnected` (la
#: personne doit se reconnecter, ce repli ne répare rien de ça), jamais un pool
#: VIDE (personne ne prête, ce n'est pas une pause, c'est une absence) — peut
#: REJOUER sur la clé de modèle API de son org, au même tier, plutôt qu'attendre
#: la réinitialisation du forfait. Ça déplace une dépense réelle d'un abonnement
#: personnel gratuit vers des jetons facturés à l'org : **décision produit,
#: assumée, jamais silencieuse** — chaque repli s'écrit dans la charge du
#: travail (`_plateforme.repli`), au même endroit que le rapport de forfait.
#:
#: ⚠️ **Narrowly ciblé sur CE cas** : un travail qui attend parce que personne ne
#: dessert la famille, ou parce que le propriétaire doit se reconnecter,
#: continue d'ATTENDRE — ce module ne touche à rien d'autre.
#:
#: Décisions du 28/09/2026 (revue de #1086) :
#: - le repli se CHOISIT : `repli_api` est FERMÉ par défaut, l'org l'ouvre ;
#: - il ne part que d'un forfait RÉELLEMENT épuisé, ou dont le seuil de l'org du
#:   TRAVAIL est dépassé (`_pause_pour_l_org`) — la pause est portée par la
#:   personne, et le seuil plus serré d'une autre org ne fait pas payer celle-ci ;
#: - il est BORNÉ : un plafond de jetons par run (`REPLI_MAX_TOKENS`, sauf plafond
#:   déclaré par l'agent) et un seul repli en vol par forfait, comme le chemin
#:   abonnement (`_deja_en_vol`) ;
#: - même modèle, même palier : Opus reste Opus.
#: ────────────────────────────────────────────────────────────────────────────

#: Le plafond de jetons d'un run replié, quand son agent n'en déclare pas. Mesuré en
#: prod le 28/09/2026 sur 30 jours de runs finis : `claude-sonnet-5` médiane 12 360,
#: p90 33 271, max 50 831 jetons ; le run le plus lourd, toutes familles, 481 206
#: (`mistral-small-2603`). 500 000 laisse dix fois le plus gros run Claude observé et
#: coupe une boucle qui s'emballe avant qu'elle ne coûte plus que quelques dollars à
#: l'org — qui n'a rien demandé de plus que « ne pas attendre ». L'agent s'arrête à
#: la fin du tour qui l'atteint (`stopped=max_tokens`).
REPLI_MAX_TOKENS = 500_000

#: Les clés de charge que la prise d'un repli réécrit, et que `defaire_le_repli` rend
#: telles qu'avant : le modèle, sa famille, ce que le catalogue attache au modèle
#: (`runner_models.charge`), et le plafond de jetons.
_CHARGE_DU_MODELE = ("model", "model_family", "effort", "max_output_tokens",
                     "max_tokens")


def _pause_pour_l_org(ab: str, org: str, seuil_defaut_pct: int) -> str:
    """SQL : l'abonnement `ab` est-il en pause POUR l'org `org` — plafond à échéance
    future, et forfait épuisé (refus du fournisseur) ou usage au-delà du seuil de
    CETTE org (`_abonnement.seuil`, relu ici : le plafond de l'org, sinon le défaut,
    resserré par le plafond perso) ? Toujours vrai ou faux, jamais NULL : un
    `bool_and` lirait un NULL comme un « oui » (une échéance ou une cause absentes).

    Une pause sans cause (écrite avant la révision `0025`) n'ouvre rien."""
    return f"""COALESCE(
        {ab}.statut = 'paused_limit' AND {ab}.limit_reset_at > NOW()
        AND ({ab}.limit_epuise
             OR {ab}.limit_utilisation >= LEAST(
                    COALESCE((SELECT lim.limite_pct FROM org_model_subscription_limits lim
                               WHERE lim.org_id = {org} AND lim.famille = {ab}.famille),
                             {int(seuil_defaut_pct)}),
                    COALESCE({ab}.limite_pct, 100)) / 100.0),
        false)"""


def _forfait_de_repli(rj: str, modep: str) -> str:
    """SQL : le FORFAIT qu'un repli remplace — celui du demandeur en mode personnel,
    le pool de l'org en mode pool. Un seul repli en vol par forfait."""
    return (f"({rj}.payload->>'model_family') || ':' || CASE WHEN {modep}.actif "
            f"THEN 'org:' || {rj}.org_id ELSE 'sub:' || {rj}.sub END")


def _jointures_de_repli(seuil_defaut_pct: int) -> str:
    """Les trois lectures qui décident si un travail `rj` est repliable : le mode de
    son org, la pause de son demandeur (mode personnel), et celle de TOUS les
    prêteurs vivants de son pool (mode pool) — la même dans la liste des candidats et
    dans la prise, qui la revérifie."""
    from .org_subscription_pool import PRET_VIVANT
    return f"""
      LEFT JOIN LATERAL (SELECT EXISTS (
               SELECT 1 FROM org_model_subscription_modes m
                WHERE m.org_id = rj.org_id
                  AND m.famille = rj.payload->>'model_family'
                  AND m.mode = 'pool') AS actif) modep ON TRUE
      LEFT JOIN LATERAL (
             SELECT ab.limit_reset_at
               FROM user_model_subscriptions ab
              WHERE NOT modep.actif AND ab.sub = rj.sub
                AND ab.famille = rj.payload->>'model_family'
                AND {_pause_pour_l_org("ab", "rj.org_id", seuil_defaut_pct)}
            ) perso ON TRUE
      LEFT JOIN LATERAL (
             SELECT MAX(ab.limit_reset_at) AS reset_at
               FROM user_model_subscription_loans l
               {PRET_VIVANT}
              WHERE modep.actif AND l.org_id = rj.org_id
                AND l.famille = rj.payload->>'model_family'
             HAVING COUNT(*) > 0
                AND bool_and({_pause_pour_l_org("ab", "rj.org_id", seuil_defaut_pct)})
            ) pool ON TRUE"""


_REPLI_EN_VOL = """SELECT 1 FROM runner_jobs vol
                    WHERE vol.status = 'claimed' AND vol.lease_until > NOW()
                      AND vol.payload->'_plateforme'->'repli'->>'forfait' = {forfait}"""


def _cle_ok_pour_repli(org_id: int, famille: str) -> bool:
    """Le repli est-il payable **par l'ORG elle-même** ? Sa propre clé déposée,
    et RIEN d'autre : pas le barreau tenant, pas la clé de plateforme, pas la clé
    d'environnement du worker.

    ⚠️ C'est la règle la plus STRICTE du dépôt, et c'est délibéré. La cascade
    ordinaire (`access.walk_cascade`) sert un travail que l'org a demandé sur un
    modèle qu'elle a choisi ; ici le travail avait choisi un ABONNEMENT — gratuit
    pour l'org — et c'est NOUS qui le déplaçons vers des jetons facturés. Déplacer
    une dépense vers quelqu'un qui ne l'a pas demandée, ce serait le faire payer
    sans qu'il ait rien signé : seule une org qui a posé SA clé a dit « je paie
    mes jetons ». Sans clé d'org, le travail continue d'ATTENDRE la
    réinitialisation du forfait — le comportement d'avant, jamais un échec dur.

    ⚠️ Ne PAS lire `runner.org_key_required` ici (ce qu'une première version
    faisait) : ce réglage est à `false` par défaut, et « non exigée » y veut dire
    « la clé d'env du worker fera l'affaire » — c'est-à-dire, sur ce chemin, que
    la plateforme paierait le repli de toutes les orgs sans clé. Le réglage
    répond à « qui peut tourner ? », cette fonction à « qui PAIE ? ».

    ⚠️ **La MÊME lecture que la REMISE** (`capabilities._cle_de_modele`), et non la
    simple présence de la ligne (`has_credential`, qui ne DÉCHIFFRE pas). Les deux
    divergent sans qu'aucune course ne soit en jeu : un coffre qui ne rend pas la
    clé — master key indisponible au boot, ligne illisible — laisse la LIGNE en
    place et fait pourtant rendre `None` à la remise. Or la remise, dans ce cas,
    **retombe sur la clé de la plateforme** (son propre commentaire le dit). On
    aurait donc rerouté un travail en jurant que l'org paie, pour nous le faire
    payer, silencieusement : le défaut même que ce lot ferme, rentré par l'autre
    porte. Ce qui DÉCIDE doit être ce qui SERT.

    Recopié plutôt qu'importé : le sens unique des 4 couches (ADR 0004) interdit à
    `db` de remonter vers `capabilities`. Vérifié AVANT la prise : rerouter vers un
    dépôt qui arrêterait ensuite le travail romprait la promesse « jamais un échec
    dur »."""
    from .. import credentials_store, providers
    c = providers.connector_for_provider(famille)
    if not c or c.kind != "credential":
        return False
    try:
        ligne = credentials_store.get_credential_with_meta("org", str(org_id), famille)
    except Exception:
        logger.warning("clé `%s` illisible pour l'org %s — repli refusé, le travail "
                       "attend son forfait", famille, org_id, exc_info=True)
        return False
    return bool(ligne and ligne.get("secret"))


def defaire_le_repli(job_id: int, worker_sub: str, raison: str,
                     delai_s: int = 60) -> bool:
    """Un travail REPLIÉ que la remise n'a finalement pas pu payer sur la clé de son
    org : on le remet EXACTEMENT comme avant le repli — son modèle d'abonnement
    retrouvé — et on le rend à la file, sans compter la tentative.

    ⚠️ Défaire le repli ET rendre à la file dans la MÊME écriture. Rendre sans
    défaire laisserait en file un travail estampillé `anthropic` : n'importe quel
    worker de cette famille le reprendrait plus tard, et si la clé de l'org n'est
    toujours pas lisible, il tournerait sur la clé d'ENVIRONNEMENT du worker — la
    nôtre. Le travail retrouve donc son `sub:*`, et redevient ce qu'il était : un
    travail qui attend la réinitialisation d'un forfait — sa charge rendue À L'OCTET
    (`charge_avant`) : ni l'effort du modèle d'arrivée, ni le plafond de jetons du
    repli ne restent sur un travail d'abonnement.

    Le `repli` de la charge n'est pas effacé mais DÉCLASSÉ en `repli_defait` (avec
    sa raison) : un repli tenté puis annulé est un fait d'exploitation, et l'effacer
    rendrait ce chemin invisible le jour où il se met à mordre en boucle."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs j
               SET status = 'pending', claimed_by = NULL, lease_until = NULL,
                   attempts = GREATEST(j.attempts - 1, 0),
                   due_at = NOW() + make_interval(secs => %s),
                   payload = jsonb_set(
                       (j.payload - %s::text[])
                       || (j.payload->'_plateforme'->'repli'->'charge_avant'),
                       '{_plateforme}',
                       (j.payload->'_plateforme') - 'repli'
                       || jsonb_build_object('repli_defait',
                              (j.payload->'_plateforme'->'repli')
                              || jsonb_build_object('raison', %s::text,
                                                    'defait_at', to_jsonb(NOW()))))
             WHERE j.id = %s AND j.claimed_by = %s AND j.status = 'claimed'
               AND j.payload->'_plateforme'->'repli'->'charge_avant' IS NOT NULL
            """,
            (int(delai_s), list(_CHARGE_DU_MODELE), raison, job_id, worker_sub))
        return bool(cur.rowcount)


def candidats_repli_abonnement(org_id: Optional[int], org_ids: Optional[list],
                               familles: frozenset, depot: str, seuil_defaut_pct: int,
                               limit: int = 25, ferme: bool = False) -> list[dict]:
    """Travaux `pending` d'une famille d'ABONNEMENT que l'org PEUT replier — lecture
    SEULE, hors verrou, pour que l'appelant choisisse une cible avant le claim
    atomique (`claim_fallback_job`, qui revérifie la MÊME pause : entre les deux, une
    reconnexion a pu gagner).

    Mode personnel : le DEMANDEUR est en pause pour l'org du travail
    (`_pause_pour_l_org`). Mode pool (réglé par l'org) : au moins un prêt VIVANT, et
    TOUS ses prêteurs le sont — un mélange avec un prêteur simplement déconnecté,
    ou servable, n'est PAS un pool « en pause », et n'ouvre aucun repli (l'état du
    demandeur ne compte jamais en pool : il ne paie pas).

    ⚠️ L'interrupteur de l'org, la PRÉSENCE d'une clé `depot` de l'org et le forfait
    déjà en vol se filtrent ICI, AVANT la limite : filtrés après, vingt-cinq travaux
    plus anciens d'orgs qui ne peuvent pas payer cacheraient pour toujours celui
    d'une org qui le peut. La clé se DÉCHIFFRE ensuite (`_cle_ok_pour_repli`) : la
    présence de la ligne ne dit pas que le coffre la rendra.

    ⚠️ Un repli vers `FAMILLE_FERME` pour une org routée vers la ferme n'est ouvert
    qu'à un worker de ferme (`ferme`) : sinon, le repli ferait passer par la boucle
    un travail que l'org a réservé à la ferme."""
    if not familles:
        return []
    hors_ferme_ok = bool(ferme) or depot != FAMILLE_FERME
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT rj.id, rj.org_id, rj.sub,
                   rj.payload->>'model' AS model,
                   rj.payload->>'model_family' AS model_family
              FROM runner_jobs rj
              {_jointures_de_repli(seuil_defaut_pct)}
             WHERE (%s::bigint IS NULL OR rj.org_id = %s)
               AND (%s::bigint[] IS NULL OR rj.org_id = ANY(%s::bigint[]))
               AND rj.status = 'pending' AND rj.due_at <= NOW()
               AND rj.attempts < rj.max_attempts
               AND rj.payload->>'model_family' = ANY(%s)
               AND (perso.limit_reset_at IS NOT NULL OR pool.reset_at IS NOT NULL)
               AND EXISTS (SELECT 1 FROM org_model_subscription_modes r
                            WHERE r.org_id = rj.org_id
                              AND r.famille = rj.payload->>'model_family'
                              AND r.repli_api)
               AND EXISTS (SELECT 1 FROM connector_credentials c
                            WHERE c.entity_type = 'org'
                              AND c.entity_id = rj.org_id::text
                              AND c.connector = %s)
               AND NOT EXISTS ({_REPLI_EN_VOL.format(
                   forfait=_forfait_de_repli("rj", "modep"))})
               AND (%s OR NOT {_org_routee_ferme("rj")})
             ORDER BY rj.due_at
             LIMIT %s
            """,
            (org_id, org_id, org_ids, org_ids, list(familles), depot, hors_ferme_ok,
             limit),
        ).fetchall()
    return [dict(r) for r in rows]


def claim_fallback_job(job_id: int, worker_sub: str, to_model: str, to_family: str,
                       seuil_defaut_pct: int,
                       lease_seconds: int = _LEASE_DEFAULT_S) -> Optional[dict]:
    """Réclame UN travail d'abonnement en pause pour le REJOUER sur `to_model`
    (famille `to_family`, une clé API) — revérifie la pause dans le WHERE de
    l'UPDATE, jamais celle lue par `candidats_repli_abonnement` : entre les deux, le
    porteur (ou le pool) a pu redevenir servable, et une reconnexion gagne toujours
    contre un repli en cours de décision.

    Réécrit la CHARGE DU MODÈLE dans la MÊME écriture que la prise
    (`runner_models.charge(to_model)` : son effort, son plafond de sortie —
    `claude-haiku-4-5` répond 400 à un effort) : tout ce qui lit le travail ensuite
    (la clé exigée, l'usage compté à la conclusion, le rapport de forfait) doit voir
    un travail `to_family` ORDINAIRE, jamais un abonnement déguisé — sans ça,
    `noter_rapport` lèverait la pause d'un abonnement qui n'a pourtant pas tourné, et
    l'usage compterait zéro jeton pour une exécution qui en a réellement consommé
    (payée par l'org). Le plafond de jetons part avec : celui que l'agent a déclaré,
    sinon `REPLI_MAX_TOKENS`.

    Un seul repli en vol par forfait : même verrou consultatif, pris APRÈS la prise,
    et même relecture que `_deja_en_vol` — deux workers qui prennent au même instant
    deux travaux du même forfait ne se voient pas dans leurs instantanés.

    Le repli se STAMPE, jamais silencieusement : `_plateforme.repli` porte l'AVANT
    (`charge_avant`, rendue telle quelle par `defaire_le_repli`), l'APRÈS, le mode,
    le forfait et l'échéance qui a déclenché le repli — visible partout où `payload`
    l'est déjà (`runner.jobs get/list`, `op=deliveries`)."""
    charge_apres = json.dumps(runner_models.charge(to_model))
    with _connect() as conn:
        conn.execute("SAVEPOINT prise_repli")
        row = conn.execute(
            f"""
            WITH cible AS (
                SELECT rj.id, rj.payload,
                       COALESCE(perso.limit_reset_at, pool.reset_at) AS reset_at,
                       CASE WHEN modep.actif THEN 'pool' ELSE 'personnel' END AS mode,
                       {_forfait_de_repli("rj", "modep")} AS forfait
                  FROM runner_jobs rj
                  {_jointures_de_repli(seuil_defaut_pct)}
                 WHERE rj.id = %s AND rj.status = 'pending' AND rj.due_at <= NOW()
                   AND rj.attempts < rj.max_attempts
                   AND (perso.limit_reset_at IS NOT NULL OR pool.reset_at IS NOT NULL)
                   AND NOT EXISTS (SELECT 1 FROM orgs o_susp WHERE o_susp.id = rj.org_id
                                    AND o_susp.suspended_at IS NOT NULL)
                 FOR UPDATE OF rj
            )
            UPDATE runner_jobs j
               SET status = 'claimed', claimed_by = %s, attempts = j.attempts + 1,
                   lease_until = NOW() + make_interval(secs => %s),
                   payload = jsonb_set(
                       (cible.payload - %s::text[]) || %s::jsonb
                       || jsonb_build_object('max_tokens', COALESCE(
                              cible.payload->'max_tokens', to_jsonb(%s::int))),
                       '{{_plateforme}}',
                       COALESCE(cible.payload->'_plateforme', '{{}}'::jsonb)
                       || jsonb_build_object('repli', jsonb_build_object(
                              'from_model', cible.payload->>'model',
                              'from_family', cible.payload->>'model_family',
                              'to_model', %s::text, 'to_family', %s::text,
                              'mode', cible.mode, 'forfait', cible.forfait,
                              'reason', 'paused_limit',
                              'reset_at', to_jsonb(cible.reset_at),
                              'charge_avant', (
                                  SELECT COALESCE(jsonb_object_agg(k, v), '{{}}'::jsonb)
                                    FROM jsonb_each(cible.payload) AS e(k, v)
                                   WHERE k = ANY(%s::text[])),
                              'at', to_jsonb(NOW())))
                   )
              FROM cible
             WHERE j.id = cible.id
            RETURNING j.id, j.kind, j.run_id, j.payload, j.attempts, j.max_attempts,
                      j.lease_until, j.sub, j.org_id
            """,
            (job_id, worker_sub, int(lease_seconds), list(_CHARGE_DU_MODELE),
             charge_apres, REPLI_MAX_TOKENS, to_model, to_family,
             list(_CHARGE_DU_MODELE)),
        ).fetchone()
        row = dict(row) if row else None
        if row:
            forfait = row["payload"]["_plateforme"]["repli"]["forfait"]
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                         (f"repli:{forfait}",))
            en_vol = conn.execute(
                _REPLI_EN_VOL.format(forfait="%s") + " AND vol.id <> %s LIMIT 1",
                (forfait, row["id"])).fetchone()
            if en_vol:
                # Un autre repli du MÊME forfait a été pris au même instant : celui-ci
                # se défait, tentative non comptée, et repartira au sondage suivant.
                conn.execute("ROLLBACK TO SAVEPOINT prise_repli")
                row = None
        conn.execute("RELEASE SAVEPOINT prise_repli")
    return row


def repli_disponible(org_id: Optional[int], org_ids: Optional[list], worker_sub: str,
                     depot: Optional[str], seuil_defaut_pct: int,
                     lease_seconds: int = _LEASE_DEFAULT_S,
                     ferme: bool = False) -> Optional[dict]:
    """Tente un repli abonnement épuisé → clé API pour le dépôt `depot` d'un worker
    de PLATEFORME (`claim_next_job` a déjà rendu `None` pour lui) : parcourt les
    candidats du PLUS ANCIEN au plus récent, s'arrête au premier repli que l'org
    PAIE (sa clé, déchiffrée) et qui gagne la course du claim. `None` si rien n'est
    repliable — l'appelant continue d'ATTENDRE, jamais un échec : un candidat sans
    clé lisible, ou perdu à la course, est SAUTÉ, pas arrêté.

    `seuil_defaut_pct` : le plafond d'une org qui n'a rien réglé
    (`_abonnement.DEFAUT_LIMITE_PCT`), passé par la capacité — la base ne remonte
    pas vers elle (ADR 0004)."""
    familles = runner_models.familles_de_repli(depot)
    if not familles:
        return None
    for cand in candidats_repli_abonnement(org_id, org_ids, familles, depot,
                                           seuil_defaut_pct, ferme=ferme):
        to_model = runner_models.repli_api(cand["model"])
        if not to_model or runner_models.famille(to_model) != depot:
            continue
        # La clé se lit sur le dépôt d'ARRIVÉE (c'est lui qui facture), DÉCHIFFRÉE
        # comme à la remise ; l'interrupteur de l'org est déjà filtré en SQL.
        if not _cle_ok_pour_repli(cand["org_id"], depot):
            continue
        row = claim_fallback_job(cand["id"], worker_sub, to_model, depot,
                                 seuil_defaut_pct, lease_seconds)
        if row:
            return row
    return None


def porteur_et_famille(job_id: int) -> Optional[dict]:
    """`{sub, org_id, model_family, abonnement}` d'un travail — de quoi adresser le
    rapport de forfait à la conclusion (OTO-130) à l'abonnement qui l'a SERVI
    (`abonnement` : le demandeur, ou le prêteur du pool), et lire le plafond que son
    ORG a réglé (`_abonnement.seuil`).

    Une lecture À PART, et non deux colonnes de plus au `RETURNING` de
    `complete_job` : ce retour est un contrat (`{status, run_id}`) que six bancs
    tiennent à l'octet, et l'élargir pour un à-côté ferait bouger tous ses
    lecteurs pour une famille qui n'en concerne qu'un."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT sub, org_id, payload->>'model_family' AS model_family, "
            f"{_FORFAIT_SQL.format(a='runner_jobs')} AS abonnement "
            "FROM runner_jobs WHERE id = %s", (job_id,)).fetchone()
    return dict(row) if row else None


def porteur_du_forfait(job: dict) -> Optional[str]:
    """Le `sub` dont l'ABONNEMENT sert ce travail pris : celui que la réservation a
    écrit (`_plateforme.abonnement`), sinon son demandeur — la lecture Python de
    `_FORFAIT_SQL`, pour la remise du sandbox."""
    plateforme = (job.get("payload") or {}).get(_CHAMP_PLATEFORME) or {}
    return plateforme.get(_CLE_FORFAIT) or job.get("sub")


def _deja_en_vol(conn, pris: dict) -> bool:
    """Un AUTRE travail servi par le même ABONNEMENT (même porteur du forfait, même
    famille) est-il en vol ? Le porteur est le demandeur en mode personnel, le
    prêteur en mode pool : une seule sérialisation pour les deux, parce qu'un bac à
    sable ne sait pas pour qui il travaille.

    ⚠️ **Pourquoi la clause `NOT EXISTS` de la réservation ne suffit pas** (mesuré
    le 21/09/2026, trois prises simultanées → deux travaux en vol) : elle lit un
    INSTANTANÉ. Deux réservations parallèles prennent chacune un travail différent
    servi par le même abonnement, et aucune ne voit la prise de l'autre — elle n'est
    pas encore committée. Même course en mode pool : deux workers choisissent le même
    prêteur, libre dans leurs deux instantanés.

    D'où le verrou consultatif, pris APRÈS la prise et BLOQUANT : les réservations
    d'un même abonnement passent une à une. La première ne voit que la sienne et
    garde ; la suivante attend le commit de la première, puis relit — en READ
    COMMITTED, chaque ordre a son instantané, donc elle VOIT la prise committée —
    et se défait. Le verrou tombe avec la transaction : aucun état à nettoyer.

    Pas d'interblocage possible : la seconde tient sa ligne et attend le verrou ;
    la première tient le verrou et n'a besoin d'aucune ligne de la seconde."""
    porteur = porteur_du_forfait(pris)
    if not porteur:
        return False
    famille = (pris.get("payload") or {}).get("model_family")
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"abonnement:{famille}:{porteur}",))
    return conn.execute(
        f"""
        SELECT 1 FROM runner_jobs j
         WHERE j.id <> %s AND j.status = 'claimed' AND j.lease_until > NOW()
           AND j.payload->>'model_family' = %s AND {_FORFAIT_SQL.format(a="j")} = %s
         LIMIT 1
        """,
        (pris["id"], famille, porteur),
    ).fetchone() is not None


def refuser_pour_identite(job_id: int, worker_sub: str, raison: str) -> bool:
    """Arrête DÉFINITIVEMENT un travail dont le porteur ne peut plus agir.

    ⚠️ Pas `complete_job(ok=False)` : celui-là refile avec backoff jusqu'au
    plafond de tentatives. **Une identité invalide ne se répare pas en
    réessayant** — on rejouerait trois fois le même refus, en trois fois plus de
    temps, pour le même verdict. Le seul effet serait de retarder le moment où
    quelqu'un le voit.

    ⚠️ Et surtout pas un relâchement silencieux : le travail repartirait au worker
    suivant, indéfiniment. Une file qui tourne sans jamais aboutir, et rien pour
    dire pourquoi — c'est exactement le trou de #814 sous une autre forme.

    `failed` et non `expired` : celui-ci a bien été PRIS, et il ne peut pas
    s'exécuter. `expired` dit « personne n'est venu le prendre », ce qui serait
    faux ici et enverrait chercher au mauvais endroit.
    """
    return arreter_definitivement(job_id, worker_sub, raison)


def rendre_a_la_file(job_id: int, worker_sub: str, raison: str,
                     delai_s: int = 60) -> bool:
    """Défait une prise SANS la compter : le travail retourne `pending`.

    L'inverse exact d'`arreter_definitivement`, pour un motif qui se RÉPARE sans
    toucher au travail — son demandeur doit se reconnecter à son abonnement
    (OTO-130). La réservation saute déjà ces personnes ; ceci ne sert qu'à la
    course où l'état change entre la prise et la garde. La tentative est rendue
    (`attempts - 1`) : le travail n'a rien tenté, et trois de ces courses ne
    doivent pas le tuer. La raison s'écrit quand même — un travail qui attend dit
    pourquoi."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs
               SET status = 'pending', claimed_by = NULL, lease_until = NULL,
                   attempts = GREATEST(attempts - 1, 0),
                   due_at = NOW() + make_interval(secs => %s), last_error = %s
             WHERE id = %s AND claimed_by = %s AND status = 'claimed'
            """,
            (int(delai_s), raison, job_id, worker_sub),
        )
        return bool(cur.rowcount)


def travaux_en_attente_d_abonnement(sub: str, famille: str) -> int:
    """Combien de travaux de cette personne attendent SA reconnexion (ou la fin de
    son plafond) — ce que l'écran annonce : « 3 travaux repartiront »."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runner_jobs "
            "WHERE sub = %s AND status = 'pending' "
            "AND payload->>'model_family' = %s",
            (sub, famille)).fetchone()
    return int(row["n"]) if row else 0


def arreter_definitivement(job_id: int, worker_sub: str, raison: str) -> bool:
    """Le geste commun à TOUT refus qui ne se répare pas en réessayant : `failed`,
    avec sa raison écrite, scopé au claimant — jamais un retour en file.

    Extrait de `refuser_pour_identite` le 12/09/2026, quand un second motif est
    apparu : un travail dont l'org n'a pas déposé la clé de modèle qu'on exige
    (`capabilities/_cle_exigee.py`). Deux fonctions portant le même UPDATE auraient
    fini par diverger ; deux NOMS sur un seul geste disent chacun leur motif."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs
               SET status = 'failed', finished_at = NOW(), last_error = %s
             WHERE id = %s AND claimed_by = %s AND status = 'claimed'
            """,
            (raison, job_id, worker_sub),
        )
        return bool(cur.rowcount)


def modele_du_run(run_id: str, org_id: int) -> dict:
    """Le modèle sous lequel un run a DÉMARRÉ — `{model, model_family}`, plus
    `effort` et `max_output_tokens` s'il en portait, ou `{}`.

    Lu sur le travail `start` du run (lié par `bind_run`). Un `continue` le reprend :
    un fil ouvert sur une voie ne se poursuit pas sur une autre — la voie
    Conversations et la boucle Messages n'ont pas le même fil. `effort` et
    `max_output_tokens` (14/09/2026) suivent la MÊME règle que `model`/`model_family` :
    ceux posés au `start`, jamais recalculés depuis le catalogue courant — un catalogue
    qui changerait entre le départ et la reprise ne doit pas faire continuer le fil sur
    un autre réglage que celui sous lequel il a commencé.

    ⚠️ Les deux voyagent ENSEMBLE : le worker lève sur un effort qui raisonne sans
    plafond (oto-runner `agent_llm_openai.plafond_de_sortie`). Relire l'un sans l'autre
    faisait lever toute reprise d'un run Medium (relevé en revue le 14/09/2026).

    ⚠️ Un run CLOS est détaché de son travail à la reprise (`claim_next_job`) : son
    `run_id` n'y figure plus. À défaut d'association courante — qui prime, comme
    avant —, le travail se retrouve par sa trace `_plateforme.runs_detaches`, dans
    CETTE org seulement. Des travaux aux modèles différents lèvent `RuntimeError` :
    en choisir un continuerait peut-être le fil sur la mauvaise voie, et `{}` le
    lancerait sur le modèle du worker. Aucun index ne couvre `payload` : la lecture
    est bornée à l'org, et ne sert que « Continuer »."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT payload->>'model' AS model,
                   payload->>'model_family' AS model_family,
                   payload->>'effort' AS effort,
                   payload->>'max_output_tokens' AS max_output_tokens
              FROM runner_jobs
             WHERE run_id = %s AND org_id = %s AND kind = 'start'
             ORDER BY id
             LIMIT 1
            """,
            (run_id, org_id),
        ).fetchone()
        if row is None:
            couples = conn.execute(
                f"""
                SELECT payload->>'model' AS model,
                       payload->>'model_family' AS model_family,
                       payload->>'effort' AS effort,
                       payload->>'max_output_tokens' AS max_output_tokens,
                       array_agg(id ORDER BY id) AS travaux
                  FROM runner_jobs
                 WHERE org_id = %s AND kind = 'start'
                   AND payload->'{_CHAMP_PLATEFORME}'->'runs_detaches'
                       @> jsonb_build_array(jsonb_build_object('run_id', %s::text))
                 GROUP BY 1, 2, 3, 4
                """,
                (org_id, run_id),
            ).fetchall()
            if len(couples) > 1:
                raise RuntimeError(
                    f"run {run_id} détaché de travaux aux modèles contradictoires — "
                    + " ; ".join(f"{c['model']}/{c['model_family']} : travaux {c['travaux']}"
                                 for c in couples)
                    + ". Aucun n'est choisi.")
            row = couples[0] if couples else None
    if not row or not row["model_family"]:
        return {}
    out = {"model": row["model"], "model_family": row["model_family"]}
    if row.get("effort"):
        out["effort"] = row["effort"]
    if row.get("max_output_tokens"):
        # `->>` rend du TEXTE : le worker attend l'entier que `charge()` avait posé.
        out["max_output_tokens"] = int(row["max_output_tokens"])
    return out


def bind_job_run(job_id: int, worker_sub: str, run_id: str) -> bool:
    """Lie un job `start` au run que le worker vient d'ouvrir — claimant seul."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs SET run_id = %s
             WHERE id = %s AND claimed_by = %s AND status = 'claimed'
            """,
            (run_id, job_id, worker_sub),
        )
        return bool(cur.rowcount)


def extend_job_lease(job_id: int, worker_sub: str,
                     lease_seconds: int = _LEASE_DEFAULT_S) -> bool:
    """Prolonge le bail — le heartbeat du worker. Claimant seul."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs
               SET lease_until = NOW() + make_interval(secs => %s)
             WHERE id = %s AND claimed_by = %s AND status = 'claimed'
            """,
            (int(lease_seconds), job_id, worker_sub),
        )
        return bool(cur.rowcount)


def complete_job(job_id: int, worker_sub: str, ok: bool,
                 error: Optional[str] = None,
                 run_id: Optional[str] = None,
                 result: Optional[dict] = None) -> Optional[dict]:
    """Conclut la prise : `done`, ou re-file avec backoff, ou `failed` au plafond.

    `result` (R5, flotte) = le résultat DÉCLARÉ par le worker (usage_tokens,
    stopped, steps…) : c'est ce qu'un ordonnanceur de flotte lit pour sa garde
    budget — jamais un secret, jamais du contenu de fil.

    Rend `{status, run_id}` conclu — `run_id` = le run que le job connaît après
    la conclusion (celui de l'appel, sinon celui posé par `bind_run`/`enqueue`,
    sinon None) : c'est la clé de la libération des baux du datastore (#633),
    lue par la capacité sans second aller-retour — ou None si le job n'est pas
    au claimant (déjà re-claimé après bail mort, ou jamais à lui) : l'appelant
    ne conclut pas ce qui ne lui appartient plus."""
    with _connect() as conn:
        if ok:
            row = conn.execute(
                """
                UPDATE runner_jobs
                   SET status = 'done', finished_at = NOW(),
                       run_id = COALESCE(%s, run_id), last_error = NULL,
                       result = COALESCE(%s::jsonb, result)
                 WHERE id = %s AND claimed_by = %s AND status = 'claimed'
                RETURNING status, run_id
                """,
                (run_id, json.dumps(result) if result is not None else None,
                 job_id, worker_sub),
            ).fetchone()
        else:
            # Échec : au plafond → failed VISIBLE ; sinon retour en file, backoff
            # linéaire, la trace d'erreur conservée pour l'audit. `result` s'écrit
            # comme au succès — la conclusion suivante l'écrase, rien ne s'additionne.
            #
            # ⚠️ `attempt_errors` s'AJOUTE (`||`), là où `last_error` écrase : les
            # tentatives d'un même travail échouent parfois pour des raisons
            # différentes, et seule la dernière survivait. Le numéro vient de
            # `attempts`, qui est déjà à jour quand on arrive ici (posé à la
            # réservation) — donc il nomme bien la tentative qu'on conclut.
            motif = (error or 'échec non détaillé')[:500]
            row = conn.execute(
                """
                UPDATE runner_jobs
                   SET status   = CASE WHEN attempts >= max_attempts
                                       THEN 'failed' ELSE 'pending' END,
                       finished_at = CASE WHEN attempts >= max_attempts
                                          THEN NOW() ELSE NULL END,
                       due_at   = NOW() + make_interval(secs => %s * attempts),
                       lease_until = NULL, claimed_by = NULL,
                       last_error = %s,
                       attempt_errors = COALESCE(attempt_errors, '[]'::jsonb)
                                        || jsonb_build_array(jsonb_build_object(
                                               'attempt', attempts,
                                               'at', to_char(NOW() AT TIME ZONE 'UTC',
                                                             'YYYY-MM-DD"T"HH24:MI:SSZ'),
                                               'error', %s::text)),
                       result = COALESCE(%s::jsonb, result)
                 WHERE id = %s AND claimed_by = %s AND status = 'claimed'
                RETURNING status, run_id
                """,
                (_BACKOFF_S, motif, motif,
                 json.dumps(result) if result is not None else None,
                 job_id, worker_sub),
            ).fetchone()
    return dict(row) if row else None


# D'OÙ vient un travail, en SQL. Le discriminant existe déjà dans la table : la
# colonne `fleet_id` pour un passage, `payload->>'trigger_id'` pour un déclencheur
# programmé, ni l'un ni l'autre pour un appel direct. Le filtre est SERVI, pas
# laissé au client : la file est paginée (`id DESC`), et un passage de 2 000 lignes
# remplit la première page à lui seul — un tri côté client rendrait « aucun travail
# programmé » sur une org qui en joue un chaque matin. Un écran qui ment sur
# l'absence est pire que pas d'écran.
_SOURCES = {
    "batch": "fleet_id IS NOT NULL",
    "scheduled": "fleet_id IS NULL AND payload->>'trigger_id' IS NOT NULL",
    "manual": "fleet_id IS NULL AND payload->>'trigger_id' IS NULL",
}


def _filtre_de_file(org_id: int, status: Optional[str],
                    source: Optional[str] = None,
                    fleet_id: Optional[int] = None,
                    trigger_id: Optional[int] = None) -> tuple[str, list]:
    """Le WHERE commun à la page et à son compte — une seule définition de « la
    file », sinon le total finit par décrire une autre population que les lignes
    qu'il accompagne. `source` et `fleet_id` en font partie pour cette raison
    exacte : servir un filtre à la page sans l'appliquer au compte redonnerait un
    total qui décrit autre chose que ce qui est affiché."""
    q = " WHERE org_id = %s"
    params: list = [org_id]
    if status:
        q += " AND status = %s"
        params.append(status)
    if source:
        clause = _SOURCES.get(source)
        if clause is None:
            raise ValueError(f"source inconnue : {source}")
        q += f" AND ({clause})"
    if fleet_id is not None:
        q += " AND fleet_id = %s"
        params.append(int(fleet_id))
    # Le déclencheur n'a pas de colonne : le tick le pose dans le payload
    # (`runner_tick.py`). Même raison que `fleet_id` — l'historique d'un
    # déclencheur trié côté client donne un total qui ne peut pas servir de
    # dénominateur, donc des taux faux sans que rien ne le signale.
    if trigger_id is not None:
        # ⚠️ Comparaison en TEXTE, jamais `::bigint`. `payload` est un JSON libre : il
        # suffit d'UNE ligne de l'org dont `trigger_id` n'est pas un nombre pour que le
        # cast fasse échouer la requête ENTIÈRE — pas seulement cette ligne-là. Le
        # filtre deviendrait alors une panne, sur des données qu'aucun de nos écrivains
        # ne produit mais que rien n'empêche d'exister.
        # La forme sûre était déjà deux fonctions plus haut (`perimer_travaux_du_
        # declencheur`, `comptage_perime`) : c'est la même clé, lue de la même façon.
        q += " AND payload->>'trigger_id' = %s"
        params.append(str(int(trigger_id)))
    return q, params


def list_jobs(org_id: int, status: Optional[str] = None,
              limit: int = 50, before_id: Optional[int] = None,
              source: Optional[str] = None,
              fleet_id: Optional[int] = None,
              trigger_id: Optional[int] = None) -> list[dict]:
    """La file vue d'en haut (surveillance dashboard) : les jobs de l'org, du
    plus récent au plus ancien, filtrables par statut. Le payload est rendu
    (références seulement, par contrat d'enqueue) mais jamais tronqué en
    silence — c'est une LISTE : elle rend de quoi écarter, le détail par get.

    ⚠️ `lease_until` en fait partie, et ce n'est pas un champ de plus : sans lui,
    « ce bail a expiré » ne se lit pas — il se DEVINE à un seuil sur l'ancienneté,
    et un seuil dérivé range dans la même case un travail lent et un travail mort.
    La colonne porte la DATE ; c'est au lecteur de la comparer à l'heure qu'il est.
    `fleet_id` dit à quel PASSAGE le travail appartient (R4).

    `before_id` = pagination keyset sur l'ordre servi (`id DESC`) : la page suivante
    est « les jobs plus anciens que celui-ci ». Un keyset plutôt qu'un OFFSET parce
    qu'une file bouge sous la marche — un job enfilé entre deux pages décalerait
    tout un OFFSET et ferait sauter une ligne.

    ⚠️ `JOBS_PAGE_MAX` reste appliqué ICI en dernier ressort, mais la borne qui
    ENGAGE est celle du contrat (`capabilities/runner_jobs.py`) : c'est elle qui la
    déclare et qui rend le total + le curseur qui la disent. Une borne connue du
    seul SQL est exactement ce que #469 reprochait."""
    ou, params = _filtre_de_file(org_id, status, source, fleet_id, trigger_id)
    q = ("SELECT id, kind, run_id, payload, status, attempts, max_attempts, "
         "       claimed_by, lease_until, last_error, attempt_errors, result, "
         "       due_at, created_at, finished_at, fleet_id, sub "
         "FROM runner_jobs") + ou
    if before_id is not None:
        q += " AND id < %s"
        params.append(int(before_id))
    q += " ORDER BY id DESC LIMIT %s"
    params.append(max(1, min(int(limit), JOBS_PAGE_MAX)))
    with _connect() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def count_jobs(org_id: int, status: Optional[str] = None,
               source: Optional[str] = None,
               fleet_id: Optional[int] = None,
               trigger_id: Optional[int] = None) -> int:
    """Le nombre de jobs de la file, MÊMES filtres que `list_jobs` et sans son
    plafond : c'est le chiffre qu'un bilan de vague vient chercher, et celui qui
    dit qu'une page est tronquée. Le curseur, lui, dit comment lire la suite.

    **Le coût a été mesuré avant d'être ajouté au chemin de la liste**, parce que ce
    dépôt a déjà gelé sa boucle sur une requête lente : sur un banc de 200 000 jobs
    répartis sur 50 orgs — ~20× le volume réel — le compte d'une org prend **7 ms**
    (seq scan parallèle) contre 3 ms pour la page. Pas d'index posé pour ça : il
    coûterait un DDL au boot et une empreinte de schéma pour économiser des
    millisecondes sur une surface de surveillance. À revoir si la file change
    d'ordre de grandeur."""
    ou, params = _filtre_de_file(org_id, status, source, fleet_id, trigger_id)
    with _connect() as conn:
        row = conn.execute("SELECT count(*) AS n FROM runner_jobs" + ou,
                           tuple(params)).fetchone()
    return int(dict(row)["n"])


def get_job(job_id: int, org_id: int) -> Optional[dict]:
    """Lecture d'un job, org-scopée — même 404 qu'un job inexistant côté capacité.

    `lease_until` est rendu comme `list_jobs` le rend, pour la même raison : la
    fiche d'un travail doit pouvoir dire si son bail court encore."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, kind, run_id, payload, status, attempts, max_attempts, result, "
            "       claimed_by, lease_until, last_error, attempt_errors, due_at, created_at, "
            "       finished_at, fleet_id, sub "
            "FROM runner_jobs WHERE id = %s AND org_id = %s",
            (job_id, org_id),
        ).fetchone()
    return dict(row) if row else None


# ── Le runner est-il ARMÉ pour cette org ? ───────────────────────────────────
# La fenêtre au-delà de laquelle un worker n'est plus tenu pour présent. Un worker
# sonde en continu (le claim revient `None` sur file vide et il repart) : quinze
# minutes laissent passer un redéploiement ou un reboot sans crier au loup.
#
# ⚠️ Le choix du sens de l'erreur est ASYMÉTRIQUE, et c'est lui qui fixe la valeur.
# Un refus à tort se répare tout seul — le message dit quoi faire, et poser le
# déclencheur trente secondes plus tard marche. Une acceptation à tort fabrique
# une promesse qui ment TOUS LES JOURS, sans une erreur, jusqu'à ce que quelqu'un
# s'aperçoive que le rapport n'arrive pas. On refuse donc du bon côté, avec une
# fenêtre assez large pour qu'un aléa d'exploitation ne la morde pas.
ARME_FENETRE_S = 15 * 60


def runner_arme(org_id: int) -> dict:
    """Ce que l'org peut dire de ses workers : présence, ancienneté, nombre.

    ⚠️ Rendu DÉCLARÉ, jamais déduit de compteurs à zéro par l'appelant : `armed`
    est un booléen que le serveur pose, et `last_seen` distingue « aucun worker
    n'est jamais venu » (None) de « il en est venu un, il y a trop longtemps ».
    Les deux appellent des gestes différents — monter un runner, ou aller voir
    pourquoi celui qui existe s'est tu.

    `families` (12/09/2026) = les familles de modèles qu'un worker de PLATEFORME
    vivant a déclarées au claim, dans la même fenêtre. ⚠️ Un worker au jeton
    d'org compte dans `workers` mais ne déclare aucune famille : il ne sert que
    les agents sans modèle (cf. `capabilities/_modele.exige_servi`). Une famille
    PORTÉE (`<famille>@org:<id>`, worker filtré par `org_ids`) ne compte que pour
    l'org qu'elle nomme.

    `farm_routed` (29/09/2026) : l'org est routée vers la ferme (`OPTION_FERME`).
    Sa famille `FAMILLE_FERME` n'est alors servie que si un worker de FERME vivant
    la sert pour elle — un worker de la boucle ne la prendra jamais."""
    with _connect() as conn:
        routee = conn.execute(
            f"SELECT {_org_routee_ferme('o')} AS r FROM (SELECT %s::bigint AS org_id) o",
            (int(org_id),),
        ).fetchone()["r"]
        familles = conn.execute(
            """
            SELECT COALESCE(array_agg(DISTINCT famille ORDER BY famille), '{}') AS f
              FROM (SELECT split_part(split_part(depot, %s, 1), %s, 1) AS famille,
                           position(%s in depot) > 0 AS de_ferme
                      FROM runner_platform_depots
                     WHERE last_seen_at > NOW() - make_interval(secs => %s)
                       AND (position(%s in depot) = 0
                            OR depot = split_part(depot, %s, 1) || %s || %s)) t
             WHERE NOT %s OR de_ferme OR famille <> %s
            """,
            (_PORTEE_ORG, _MARQUE_FERME[0], _MARQUE_FERME, ARME_FENETRE_S,
             _PORTEE_ORG, _PORTEE_ORG, _PORTEE_ORG, str(int(org_id)),
             bool(routee), FAMILLE_FERME),
        ).fetchone()
        row = conn.execute(
            """
            SELECT COUNT(*) FILTER (
                       WHERE last_seen_at > NOW() - make_interval(secs => %s)
                   ) AS vivants,
                   MAX(last_seen_at) AS dernier
              FROM (
                   SELECT last_seen_at FROM runner_workers WHERE org_id = %s
                    UNION ALL
                   -- Un worker de PLATEFORME sert toutes les orgs : il compte
                   -- comme présent pour celle-ci. Sans ce bras, une org servie
                   -- uniquement par lui se lirait « aucun runner » et son
                   -- premier déclencheur serait refusé pour rien.
                   SELECT last_seen_at FROM runner_platform_workers
                   ) t
            """,
            (ARME_FENETRE_S, org_id),
        ).fetchone()
    vivants = int(row["vivants"] or 0) if row else 0
    dernier = row["dernier"] if row else None
    return {"armed": vivants > 0,
            "workers": vivants,
            "last_seen": str(dernier) if dernier else None,
            "families": list(familles["f"] or []) if familles else [],
            "farm_routed": bool(routee)}
