"""Le catalogue des causes de panne d'agent : une empreinte rangée une fois pour toutes
les orgs.

Une table NEUVE, née entière : `agent_causes` (`db/schema/runs.py::AGENT_CAUSES`) —
posée aussi par le démarrage (`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL.
Aucune FK, aucune colonne de compte.

Et une colonne NEUVE, nullable, sans défaut, sur `agent_episodes` : `empreinte` (la clé de
la cause de l'épisode) — posée aussi par le démarrage, sous garde de catalogue, même DDL
(`db/causes_agents.py::DDL_COLONNE_EMPREINTE`).

**Verrous.** `AccessExclusiveLock` bref sur `agent_episodes` (colonne nullable sans
défaut : catalogue seul, aucune réécriture) ; la table neuve est vide. `lock_timeout` 5 s :
le médecin des agents lit ces épisodes en continu ; un échec net, qu'on rejoue, plutôt
qu'une attente qui le bloquerait.

**Ordre avec le code : indifférent.** Le démarrage pose la même table et la même colonne ;
l'ancien code les ignore.

Retour arrière : retire la colonne et la table (le catalogue et ses décisions se perdent ;
les épisodes restent).

Révision : 20261113_1830_causes_agents
Précédente : 20261113_1800_usage_org
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import causes_agents
from oto_mcp.db.schema.runs import AGENT_CAUSES

revision = "20261113_1830_causes_agents"
down_revision = "20261113_1800_usage_org"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(causes_agents.DDL_COLONNE_EMPREINTE)
    op.execute(AGENT_CAUSES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS agent_causes")
    op.execute(f"ALTER TABLE agent_episodes DROP COLUMN IF EXISTS "
               f"{causes_agents.COLONNE_EMPREINTE}")
