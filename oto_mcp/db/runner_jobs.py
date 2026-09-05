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
from typing import Any, Optional

from ._conn import _connect

# Backoff linéaire simple : un échec renvoie le job dans la file à +30 s × tentatives.
# Pas d'exponentiel en V1 — les échecs attendus (amont LLM en vrac) se lissent, et un
# job vraiment cassé atteint son plafond en minutes, pas en heures.
_BACKOFF_S = 30
_LEASE_DEFAULT_S = 600  # ~3× la ligne la plus lente mesurée (180 s) — le tour d'un run

# Plafond d'une page de file. Il existait déjà — enfoui dans le LIMIT, appliqué sans
# être dit : `limit=1000` rendait 200 lignes et rien ne l'annonçait (#469). Il est
# nommé ici parce que c'est là que le SQL le fait respecter, et déclaré au contrat
# par la capacité, qui rend le total et le curseur sans lesquels une page pleine est
# indiscernable d'une file épuisée.
JOBS_PAGE_MAX = 200


def enqueue_job(org_id: int, kind: str, payload: Optional[dict] = None,
                run_id: Optional[str] = None, max_attempts: int = 3,
                fleet_id: Optional[int] = None,
                sub: Optional[str] = None) -> dict:
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
    l'état d'un passage étranger.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO runner_jobs (org_id, kind, payload, run_id, max_attempts,
                                     fleet_id, sub)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)
            RETURNING id, status, due_at, fleet_id, sub
            """,
            (org_id, kind,
             json.dumps(payload, ensure_ascii=False) if payload is not None else None,
             run_id, max(1, int(max_attempts)), fleet_id, sub),
        ).fetchone()
    return dict(row)


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
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runner_jobs
               SET status = 'expired', finished_at = NOW(),
                   last_error = %s
             WHERE org_id = %s AND status = 'pending'
               AND payload->>'trigger_id' = %s
            """,
            (raison, org_id, str(trigger_id)),
        )
        return cur.rowcount or 0


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


def claim_next_job(org_id: int, worker_sub: str,
                   lease_seconds: int = _LEASE_DEFAULT_S) -> Optional[dict]:
    """Le prochain job de l'org, bail posé — ou None (file vide).

    Marque d'abord `failed` les épaves (bail mort + tentatives épuisées) : elles
    deviennent VISIBLES au lieu d'être re-servies pour rien."""
    with _connect() as conn:
        # Le SONDAGE vaut présence — avant même de savoir s'il y a du travail. Un
        # claim sur file vide n'écrit rien d'autre : sans cette ligne, une org
        # servie par un worker bien vivant serait indistinguable d'une org sans
        # runner, et `runner_arme` refuserait le premier déclencheur pour rien.
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
             WHERE org_id = %s AND status = 'claimed'
               AND lease_until < NOW() AND attempts >= max_attempts
            """,
            (org_id,),
        )
        row = conn.execute(
            """
            UPDATE runner_jobs j
               SET status = 'claimed', claimed_by = %s, attempts = j.attempts + 1,
                   lease_until = NOW() + make_interval(secs => %s)
             WHERE j.id = (
                   SELECT id FROM runner_jobs
                    WHERE org_id = %s AND due_at <= NOW()
                      AND (status = 'pending'
                           OR (status = 'claimed' AND lease_until < NOW()))
                      AND attempts < max_attempts
                    ORDER BY due_at
                      FOR UPDATE SKIP LOCKED
                    LIMIT 1)
            RETURNING id, kind, run_id, payload, attempts, max_attempts,
                      lease_until, sub, org_id
            """,
            (worker_sub, int(lease_seconds), org_id),
        ).fetchone()
    return dict(row) if row else None


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
            # linéaire, la trace d'erreur conservée pour l'audit.
            row = conn.execute(
                """
                UPDATE runner_jobs
                   SET status   = CASE WHEN attempts >= max_attempts
                                       THEN 'failed' ELSE 'pending' END,
                       finished_at = CASE WHEN attempts >= max_attempts
                                          THEN NOW() ELSE NULL END,
                       due_at   = NOW() + make_interval(secs => %s * attempts),
                       lease_until = NULL, claimed_by = NULL,
                       last_error = %s
                 WHERE id = %s AND claimed_by = %s AND status = 'claimed'
                RETURNING status, run_id
                """,
                (_BACKOFF_S, (error or 'échec non détaillé')[:500], job_id, worker_sub),
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
         "       claimed_by, lease_until, last_error, result, due_at, created_at, "
         "       finished_at, fleet_id, sub "
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
            "       claimed_by, lease_until, last_error, due_at, created_at, "
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
    pourquoi celui qui existe s'est tu."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FILTER (
                       WHERE last_seen_at > NOW() - make_interval(secs => %s)
                   ) AS vivants,
                   MAX(last_seen_at) AS dernier
              FROM runner_workers
             WHERE org_id = %s
            """,
            (ARME_FENETRE_S, org_id),
        ).fetchone()
    vivants = int(row["vivants"] or 0) if row else 0
    dernier = row["dernier"] if row else None
    return {"armed": vivants > 0,
            "workers": vivants,
            "last_seen": str(dernier) if dernier else None}
