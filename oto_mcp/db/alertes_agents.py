"""Les agents programmés BLOQUÉS — leur relevé et la marque de l'épisode annoncé.

Un agent dont les travaux meurent l'un après l'autre ne prévient personne : sa file se
vide, son écran le dit « actif », et la panne se découvre en cherchant autre chose
(signal 1168 : une routine arrêtée 33 passages de suite sur une clé refusée, sans
personne de prévenu). `usage.agents_en_echec` rend ces pannes LISIBLES ; ce module les
rend ANNONÇABLES, une fois par épisode (`alerte_agents_bloques`).

**L'épisode** = les travaux morts d'un agent depuis son dernier travail réussi — donc
consécutifs ; mort et réussi au sens de `issues_des_travaux` (un `done` que l'agent a
conclu `blocked` est mort). Il s'ouvre quand il atteint le seuil, il se clôt au premier
succès. `runner_triggers.echec_annonce_at` dit qu'il a été annoncé : un épisode est
annoncé si cette date est postérieure à son dernier succès (ou s'il n'y en a aucun).
Aucune remise à zéro à écrire : le succès suivant suffit à rouvrir la porte.

⚠️ La marque n'est PAS une écriture de configuration : elle ne date pas l'agent
(`updated_at`, `db/runner_triggers.py`).
"""
from __future__ import annotations

from typing import Any

from ._conn import _connect
from .issues_des_travaux import MORT, MOTIF, REUSSI

# La date de l'annonce du dernier épisode de blocage d'un agent : posée par la révision
# `20261023_1040_echec_annonce` ET par le démarrage, sous garde de catalogue —
# même DDL. NULL = aucun épisode annoncé.
COLONNE_ECHEC_ANNONCE = "echec_annonce_at"
DDL_COLONNE_ECHEC_ANNONCE = (f"ALTER TABLE runner_triggers ADD COLUMN IF NOT EXISTS "
                             f"{COLONNE_ECHEC_ANNONCE} TIMESTAMPTZ")

#: Les travaux morts que l'on regarde : ceux de la dernière semaine. Un épisode plus
#: ancien dont rien n'a bougé depuis n'est plus une panne qui court.
FENETRE_JOURS = 7

#: Ce qu'on garde du dernier motif pour le courriel.
MOTIF_TETE = 200

#: Le discriminant d'un travail de déclencheur, écrit comme l'index partiel
#: `idx_runner_jobs_declencheurs` (`runner_jobs.INDEX_DECLENCHEURS`) pour qu'il serve.
_DU_DECLENCHEUR = ("j.org_id = t.org_id AND j.payload->>'trigger_id' ~ '^[0-9]+$' "
                   "AND j.payload->>'trigger_id' = t.id::text")


def agents_bloques(seuil: int) -> list[dict[str, Any]]:
    """Les agents ALLUMÉS dont l'épisode de blocage atteint `seuil` travaux morts
    consécutifs et n'a pas encore été annoncé. Une ligne par agent : son org, son
    libellé, le compte, la date du premier et du dernier travail mort, le dernier motif."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            SELECT t.id AS trigger_id, t.org_id,
                   COALESCE(NULLIF(t.label, ''), t.procedure) AS libelle,
                   f.travaux, f.depuis, f.dernier, f.motif
              FROM runner_triggers t
              CROSS JOIN LATERAL (
                    SELECT MAX(j.finished_at) AS dernier_succes
                      FROM runner_jobs j
                     WHERE {_DU_DECLENCHEUR} AND {REUSSI}) s
              CROSS JOIN LATERAL (
                    SELECT COUNT(*)::int AS travaux,
                           MIN(j.finished_at) AS depuis, MAX(j.finished_at) AS dernier,
                           (array_agg(LEFT({MOTIF}, %(tete)s)
                                      ORDER BY j.finished_at DESC))[1] AS motif
                      FROM runner_jobs j
                     WHERE {_DU_DECLENCHEUR} AND {MORT}
                       AND j.finished_at > NOW() - make_interval(days => %(fenetre)s)
                       AND (s.dernier_succes IS NULL
                            OR j.finished_at > s.dernier_succes)) f
             WHERE t.enabled
               AND f.travaux >= %(seuil)s
               AND (t.{COLONNE_ECHEC_ANNONCE} IS NULL
                    OR (s.dernier_succes IS NOT NULL
                        AND t.{COLONNE_ECHEC_ANNONCE} < s.dernier_succes))
             ORDER BY t.org_id, t.id
            """,
            {"seuil": max(1, int(seuil)), "fenetre": FENETRE_JOURS, "tete": MOTIF_TETE},
        ).fetchall()]


def marquer_annonces(trigger_ids: list) -> int:
    """Pose `echec_annonce_at` sur les agents annoncés — APRÈS l'envoi, jamais avant :
    marquer d'abord ferait d'un envoi raté un silence définitif. Rend le nombre marqué."""
    if not trigger_ids:
        return 0
    with _connect() as conn:
        return conn.execute(
            f"UPDATE runner_triggers SET {COLONNE_ECHEC_ANNONCE} = NOW() "
            "WHERE id = ANY(%s)", ([int(i) for i in trigger_ids],)).rowcount or 0
