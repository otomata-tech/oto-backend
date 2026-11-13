"""Les alertes d'agents d'une org sur Slack — le réglage, et ce qui est déjà parti (09/11/2026).

Le réglage (`agent_alert_settings`, une ligne par org) dit OÙ prévenir : un canal, ou, sans
canal, la personne qui a connecté Slack en message privé. Il naît ALLUMÉ à la connexion de
Slack (`activer_a_la_connexion`, appelé par `auth/slack.finish`) — sans écraser un réglage
qu'un admin a posé.

Ce qui est parti s'écrit sur l'ÉPISODE (`agent_episodes`), trois dates, une par message :
`slack_alerte_at` (l'agent ne fonctionne plus), `slack_repare_at` (il a été réparé),
`slack_retour_at` (il refonctionne). Chacune posée APRÈS l'envoi — marquer d'abord ferait
d'un envoi raté un silence définitif (même règle que `alertes_agents.marquer_annonces`).

Et `message_client` (JSONB, sur l'épisode) : ce qu'on dit au client, écrit par l'agent
médecin (`oto_admin_agent_repair op=diagnose`, champs `client_*`) dans la langue de l'espace
— `{headline, todo, action, link}`. Sans lui, une phrase par classe (`alertes_slack_agents`).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ._conn import _connect

DDL_EPISODES = (
    "ALTER TABLE agent_episodes ADD COLUMN IF NOT EXISTS message_client JSONB",
    "ALTER TABLE agent_episodes ADD COLUMN IF NOT EXISTS slack_alerte_at TIMESTAMPTZ",
    "ALTER TABLE agent_episodes ADD COLUMN IF NOT EXISTS slack_repare_at TIMESTAMPTZ",
    "ALTER TABLE agent_episodes ADD COLUMN IF NOT EXISTS slack_retour_at TIMESTAMPTZ",
)
COLONNES_EPISODES = ("message_client", "slack_alerte_at", "slack_repare_at",
                     "slack_retour_at")

_REGLAGE = "org_id, enabled, channel_id, channel_name, dm_user, dm_name, updated_at"


def reglage(org_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        r = conn.execute(f"SELECT {_REGLAGE} FROM agent_alert_settings WHERE org_id = %s",
                         (int(org_id),)).fetchone()
    return dict(r) if r else None


def poser(org_id: int, *, enabled: bool, channel_id: Optional[str],
          channel_name: Optional[str]) -> dict[str, Any]:
    """Le geste d'un admin : allumé ou non, et le canal (None = message privé à la
    personne qui a connecté Slack — gardée telle quelle)."""
    with _connect() as conn:
        return dict(conn.execute(
            f"""
            INSERT INTO agent_alert_settings (org_id, enabled, channel_id, channel_name)
            VALUES (%(org)s, %(on)s, %(cid)s, %(cname)s)
            ON CONFLICT (org_id) DO UPDATE
               SET enabled = EXCLUDED.enabled, channel_id = EXCLUDED.channel_id,
                   channel_name = EXCLUDED.channel_name, updated_at = NOW()
            RETURNING {_REGLAGE}
            """,
            {"org": int(org_id), "on": bool(enabled), "cid": channel_id or None,
             "cname": channel_name or None}).fetchone())


def activer_a_la_connexion(org_id: int, *, dm_user: Optional[str],
                           dm_name: Optional[str]) -> dict[str, Any]:
    """Slack vient d'être connecté pour l'org : les alertes s'allument, en message privé à
    cette personne — SAUF si un réglage existe déjà (un admin a choisi, on ne le défait
    pas) ; on y complète seulement la personne du message privé si elle manquait."""
    with _connect() as conn:
        return dict(conn.execute(
            f"""
            INSERT INTO agent_alert_settings (org_id, enabled, dm_user, dm_name)
            VALUES (%(org)s, TRUE, %(dm)s, %(nom)s)
            ON CONFLICT (org_id) DO UPDATE
               SET dm_user = COALESCE(agent_alert_settings.dm_user, EXCLUDED.dm_user),
                   dm_name = COALESCE(agent_alert_settings.dm_name, EXCLUDED.dm_name)
            RETURNING {_REGLAGE}
            """,
            {"org": int(org_id), "dm": dm_user or None, "nom": dm_name or None}).fetchone())


def orgs_a_prevenir() -> dict[int, dict[str, Any]]:
    """Les orgs dont les alertes Slack sont allumées et qui ont où les envoyer."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_REGLAGE} FROM agent_alert_settings WHERE enabled "
            "AND (channel_id IS NOT NULL OR dm_user IS NOT NULL)").fetchall()
    return {int(r["org_id"]): dict(r) for r in rows}


def poser_message_client(episode_id: int, message: dict[str, Any]) -> None:
    """Ce qu'on dit au client sur cet épisode — remplacé en entier, champs vides retirés."""
    propre = {k: v for k, v in message.items() if v}
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET message_client = %s::jsonb WHERE id = %s",
                     (json.dumps(propre) if propre else None, int(episode_id)))


def a_annoncer(org_ids: list[int], attente_min: int = 120) -> list[dict[str, Any]]:
    """Les épisodes de ces orgs qui ont un message à envoyer, avec leur agent :
    - `alerte` : vivant, côté titulaire, jamais annoncé, pas réparé — dès que l'agent
      médecin a écrit le message du client ou que sa cause APPROUVÉE du même côté a sa
      copie (`agent_causes`, `cause_copie`), sinon après `attente_min` minutes (le temps
      de le laisser réparer ce qui se répare) ;
    - `repare` : une réparation dite au client (`runner_triggers.repair_note`) plus
      récente que l'ouverture de l'épisode et jamais annoncée ;
    - `retour` : clos guéri, après une alerte, sans retour annoncé."""
    if not org_ids:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.org_id, e.trigger_id, e.classe, e.cote, e.statut, e.motif,
                   e.diagnostic, e.message_client, e.ouvert_at, e.clos_at, e.resolution,
                   e.slack_alerte_at, e.slack_repare_at, e.slack_retour_at,
                   COALESCE(NULLIF(t.label, ''), t.procedure) AS libelle,
                   t.repair_note, t.sub AS agent_sub, e.empreinte,
                   c.copie AS cause_copie, c.cote AS cause_cote,
                   CASE
                     WHEN e.statut <> 'clos' AND e.cote = 'proprietaire'
                          AND e.slack_alerte_at IS NULL
                          AND NOT (t.repair_note IS NOT NULL
                                   AND (t.repair_note->>'at')::timestamptz >= e.ouvert_at)
                          AND (e.message_client IS NOT NULL
                               OR (c.copie IS NOT NULL AND c.cote = e.cote)
                               OR e.ouvert_at < NOW() - %s * INTERVAL '1 minute')
                          THEN 'alerte'
                     WHEN e.slack_repare_at IS NULL AND t.repair_note IS NOT NULL
                          AND (t.repair_note->>'at')::timestamptz >= e.ouvert_at
                          THEN 'repare'
                     WHEN e.statut = 'clos' AND e.resolution = 'gueri'
                          AND e.slack_alerte_at IS NOT NULL AND e.slack_retour_at IS NULL
                          AND e.slack_repare_at IS NULL THEN 'retour'
                   END AS message
              FROM agent_episodes e
              JOIN runner_triggers t ON t.id = e.trigger_id
              LEFT JOIN agent_causes c ON c.cle = e.empreinte AND c.statut = 'approuvee'
             WHERE e.org_id = ANY(%s)
               AND (e.statut <> 'clos' OR e.clos_at > NOW() - INTERVAL '1 day')
             ORDER BY e.org_id, e.id
            """,
            (int(attente_min), [int(o) for o in org_ids])).fetchall()
    return [dict(r) for r in rows if r["message"]]


def marquer(episode_id: int, message: str) -> None:
    """Pose la date du message parti — APRÈS l'envoi."""
    colonne = {"alerte": "slack_alerte_at", "repare": "slack_repare_at",
               "retour": "slack_retour_at"}[message]
    with _connect() as conn:
        conn.execute(f"UPDATE agent_episodes SET {colonne} = NOW() WHERE id = %s",
                     (int(episode_id),))


def problemes_vivants(org_id: int, trigger_ids: list[int]) -> dict[int, dict[str, Any]]:
    """L'épisode VIVANT de chacun de ces agents (s'il en a un) : de quoi servir `problem`
    sur leurs travaux — par agent : `dernier_job_id`, `cote`, `classe`, `motif`,
    `message_client`, et la copie de sa cause approuvée (`cause_copie`, `cause_cote`)."""
    if not trigger_ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT e.trigger_id, e.dernier_job_id, e.cote, e.classe, e.motif, "
            "e.diagnostic, e.message_client, e.empreinte, c.copie AS cause_copie, "
            "c.cote AS cause_cote FROM agent_episodes e "
            "LEFT JOIN agent_causes c ON c.cle = e.empreinte AND c.statut = 'approuvee' "
            "WHERE e.org_id = %s AND e.trigger_id = ANY(%s) AND e.statut <> 'clos'",
            (int(org_id), [int(t) for t in trigger_ids])).fetchall()
    return {int(r["trigger_id"]): dict(r) for r in rows}
