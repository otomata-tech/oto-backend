"""« Laisser Tulina réparer mes agents » : le réglage d'une org.

Une table NEUVE, née entière : `agent_health_settings` (`db/schema/runs.py::
AGENT_HEALTH_SETTINGS`) — posée aussi par le démarrage (`CREATE TABLE IF NOT EXISTS` de
l'assemblage), même DDL. Une ligne par org qui a choisi ; aucune ligne = allumé. Aucune
FK, aucune colonne de compte.

**Verrous.** La table est neuve et vide. `lock_timeout` 5 s : un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose la même table ; l'ancien code
l'ignore.

Retour arrière : retire la table (les choix des orgs se perdent : toutes reviennent au
défaut, allumé).

Révision : 20261113_1900_sante_agents
Précédente : 20261113_1830_causes_agents
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.runs import AGENT_HEALTH_SETTINGS

revision = "20261113_1900_sante_agents"
down_revision = "20261113_1830_causes_agents"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(AGENT_HEALTH_SETTINGS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS agent_health_settings")
