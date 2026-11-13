"""La SANTÉ des agents d'une org : ce que lit l'onglet Santé, et le réglage « laisser
Tulina réparer mes agents » (13/11/2026).

Une lecture par org (`episodes_vivants`, `reparations_recentes`), jamais une par agent :
l'onglet d'une org de deux cents agents coûte deux requêtes. Le réglage (`auto_repair`,
`agent_health_settings`) naît ALLUMÉ : aucune ligne = allumé.
"""
from __future__ import annotations

from typing import Any, Iterable

from ._conn import _connect


def auto_repair(org_id: int) -> bool:
    with _connect() as conn:
        r = conn.execute("SELECT auto_repair FROM agent_health_settings WHERE org_id = %s",
                         (int(org_id),)).fetchone()
    return True if r is None else bool(r["auto_repair"])


def poser_auto_repair(org_id: int, valeur: bool) -> bool:
    with _connect() as conn:
        return bool(conn.execute(
            """
            INSERT INTO agent_health_settings (org_id, auto_repair) VALUES (%s, %s)
            ON CONFLICT (org_id) DO UPDATE
               SET auto_repair = EXCLUDED.auto_repair, updated_at = NOW()
            RETURNING auto_repair
            """, (int(org_id), bool(valeur))).fetchone()["auto_repair"])


def orgs_sans_auto_repair(org_ids: Iterable[int]) -> set[int]:
    """Parmi ces orgs, celles qui ont éteint la réparation : une requête pour un passage."""
    ids = sorted({int(o) for o in org_ids})
    if not ids:
        return set()
    with _connect() as conn:
        return {int(r["org_id"]) for r in conn.execute(
            "SELECT org_id FROM agent_health_settings "
            "WHERE org_id = ANY(%s) AND NOT auto_repair", (ids,)).fetchall()}


def episodes_vivants(org_id: int) -> list[dict[str, Any]]:
    """Les épisodes vivants des agents ALLUMÉS de l'org, avec la copie de leur cause
    approuvée — de quoi dire au client ce qui l'attend."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT e.id, e.org_id, e.trigger_id, e.classe, e.cote, e.statut, e.motif,
                   e.message_client, e.ouvert_at, e.empreinte,
                   c.copie AS cause_copie, c.cote AS cause_cote
              FROM agent_episodes e
              JOIN runner_triggers t ON t.id = e.trigger_id AND t.enabled
              LEFT JOIN agent_causes c ON c.cle = e.empreinte AND c.statut = 'approuvee'
             WHERE e.org_id = %s AND e.statut <> 'clos'
             ORDER BY e.ouvert_at
            """, (int(org_id),)).fetchall()]


def reparations_recentes(org_id: int, *, jours: int = 30,
                         limit: int = 50) -> list[dict[str, Any]]:
    """Les épisodes de l'org GUÉRIS après un geste du médecin dans les `jours` derniers
    jours, le plus récent d'abord : ce que Tulina a réparé. Avec la note de réparation
    de l'agent (`repair_note`) quand elle date de l'épisode."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT e.id, e.trigger_id, e.clos_at, e.reparations,
                   CASE WHEN t.repair_note IS NOT NULL
                         AND (t.repair_note->>'at')::timestamptz >= date_trunc('second', e.ouvert_at)
                        THEN t.repair_note END AS repair_note
              FROM agent_episodes e
              LEFT JOIN runner_triggers t ON t.id = e.trigger_id
             WHERE e.org_id = %s AND e.statut = 'clos' AND e.resolution = 'gueri'
               AND jsonb_array_length(e.reparations) > 0
               AND e.clos_at > NOW() - make_interval(days => %s)
             ORDER BY e.clos_at DESC
             LIMIT %s
            """, (int(org_id), int(jours), max(1, min(int(limit), 100)))).fetchall()]
