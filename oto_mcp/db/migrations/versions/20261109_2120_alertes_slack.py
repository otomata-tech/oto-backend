"""Les alertes d'agents d'une org sur Slack : où les envoyer, et ce qui est déjà parti.

Colonnes NEUVES, nullables, sans défaut, sur `agent_episodes` — posées aussi par le
démarrage, sous garde de catalogue, même DDL (`db/alertes_slack.py::DDL_EPISODES`) :
`message_client` (ce qu'on dit au client, écrit par l'agent médecin), `slack_alerte_at`,
`slack_repare_at`, `slack_retour_at` (les messages Slack partis).

Et une table NEUVE, née entière : `agent_alert_settings` (`db/schema/runs.py::
AGENT_ALERT_SETTINGS`) — où une org est prévenue. Aucune colonne de compte.

**Verrous.** `AccessExclusiveLock` bref sur `agent_episodes` (colonnes nullables sans
défaut : catalogue seul, aucune réécriture) ; la table neuve est vide. `lock_timeout` 5 s :
le médecin des agents lit ces épisodes en continu ; un échec net, qu'on rejoue, plutôt
qu'une attente qui le bloquerait.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes colonnes et la même
table ; l'ancien code les ignore.

Retour arrière : retire les colonnes et la table (les messages au client, les dates
d'envoi et les réglages d'alertes se perdent ; les épisodes restent).

Révision : 20261109_2120_alertes_slack
Précédente : 20261109_2110_note_de_reparation
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import alertes_slack
from oto_mcp.db.schema.runs import AGENT_ALERT_SETTINGS

revision = "20261109_2120_alertes_slack"
down_revision = "20261109_2110_note_de_reparation"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for ddl in alertes_slack.DDL_EPISODES:
        op.execute(ddl)
    op.execute(AGENT_ALERT_SETTINGS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS agent_alert_settings")
    for col in alertes_slack.COLONNES_EPISODES:
        op.execute(f"ALTER TABLE agent_episodes DROP COLUMN IF EXISTS {col}")
