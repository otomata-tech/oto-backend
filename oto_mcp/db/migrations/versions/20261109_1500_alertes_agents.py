"""Les agents qui s'arrêtent sans échouer, leurs réparations, et les alertes Slack d'une org
(09/11/2026).

Colonnes NEUVES, nullables, sans défaut — chacune posée aussi par le démarrage, sous garde
de catalogue, même DDL :
- `runner_jobs.run_outcome`, `runner_jobs.run_note` (`db/issues_des_travaux.py`) : l'issue
  que l'agent a conclue de son travail (`run_finish`), et sa note ;
- `runner_triggers.repair_note` (`db/runner_triggers.py`) : la dernière réparation du
  médecin des agents, dite au client ;
- `agent_episodes.message_client`, `slack_alerte_at`, `slack_repare_at`, `slack_retour_at`
  (`db/alertes_slack.py`) : ce qu'on dit au client, et les messages Slack partis.

Et une table NEUVE, née entière : `agent_alert_settings` (`db/schema/runs.py::
AGENT_ALERT_SETTINGS`) — où une org est prévenue.

**Verrous.** `AccessExclusiveLock` bref sur `runner_jobs`, `runner_triggers` et
`agent_episodes` (colonnes nullables sans défaut : catalogue seul, aucune réécriture) ;
la table neuve est vide. `lock_timeout` 5 s : le tick et la prise lisent ces tables en
continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui les bloquerait.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes colonnes et la même
table ; l'ancien code les ignore.

Retour arrière : retire les colonnes et la table (les issues déclarées, les réparations
dites et les réglages d'alertes se perdent ; les épisodes restent).

Révision : 20261109_1500_alertes_agents
Précédente : 20261108_1200_procedure_comments
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import alertes_slack, issues_des_travaux, runner_triggers
from oto_mcp.db.schema.runs import AGENT_ALERT_SETTINGS

revision = "20261109_1500_alertes_agents"
down_revision = "20261108_1200_procedure_comments"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(issues_des_travaux.DDL_COLONNE_ISSUE)
    op.execute(issues_des_travaux.DDL_COLONNE_NOTE)
    op.execute(runner_triggers.DDL_COLONNE_REPARATION)
    for ddl in alertes_slack.DDL_EPISODES:
        op.execute(ddl)
    op.execute(AGENT_ALERT_SETTINGS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS agent_alert_settings")
    for col in alertes_slack.COLONNES_EPISODES:
        op.execute(f"ALTER TABLE agent_episodes DROP COLUMN IF EXISTS {col}")
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS "
               f"{runner_triggers.COLONNE_REPARATION}")
    op.execute(f"ALTER TABLE runner_jobs DROP COLUMN IF EXISTS {issues_des_travaux.COLONNE_NOTE}")
    op.execute(f"ALTER TABLE runner_jobs DROP COLUMN IF EXISTS {issues_des_travaux.COLONNE_ISSUE}")
