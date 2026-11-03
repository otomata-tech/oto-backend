"""L'agent intégré dans Slack : où vit sa conversation, et les messages déjà pris en charge
(docs/copilot.md §Dans Slack).

Deux tables NEUVES et deux index — le DDL est le fragment `db/schema/copilot.py::SLACK`,
exécuté tel quel : `copilot_slack_threads (thread_id → copilot_threads ON DELETE CASCADE,
team_id, channel, thread_ts, sub, created_at)` avec un index unique `(team_id, channel,
thread_ts, sub)`, et `copilot_slack_events (channel, ts, created_at)` clé `(channel, ts)`
avec un index de purge sur `created_at`.

**Verrous.** `AccessExclusiveLock` sur les tables neuves (vides, instantané) ; la clé
étrangère prend un `ShareRowExclusiveLock` BREF sur `copilot_threads` (aucune réécriture,
aucun parcours : la table neuve est vide). `lock_timeout` 5 s.

**Ordre avec le code : indifférent.** Le démarrage pose le même fragment (`CREATE TABLE IF
NOT EXISTS`) ; l'ancien code l'ignore.

Retour arrière : retire les deux tables (une conversation de Slack n'est plus retrouvée
par son fil ; la conversation elle-même, dans `copilot_threads`, reste).

Révision : 20261103_1000_copilot_slack
Précédente : 20261102_1000_higgsfield_jobs
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.copilot import SLACK

revision = "20261103_1000_copilot_slack"
down_revision = "20261102_1000_higgsfield_jobs"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(SLACK)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS copilot_slack_events")
    op.execute("DROP TABLE IF EXISTS copilot_slack_threads")
