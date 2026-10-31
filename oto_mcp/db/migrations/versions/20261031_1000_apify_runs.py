"""Les runs Apify de la clé plateforme, notés au lancement (`db/apify_runs.py`).

Une table NEUVE, née entière, et trois index — le DDL est le fragment
`db/schema/usage.py::APIFY_RUNS`, exécuté tel quel.

**Verrous.** `AccessExclusiveLock` sur la table neuve seulement (vide, instantané),
aucune clé étrangère. `lock_timeout` 5 s par habitude de la maison.

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE IF
NOT EXISTS`, même fragment) ; l'ancien code l'ignore. Le lancement d'un run n'en dépend
pas : une écriture ratée est journalisée, le run part quand même.

Retour arrière : retire la table (les runs non relevés redeviennent invisibles, le coût
de ceux déjà relevés reste dans le journal).

Révision : 20261031_1000_apify_runs
Précédente : 20261031_0040_agent_preset
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.usage import APIFY_RUNS

revision = "20261031_1000_apify_runs"
down_revision = "20261031_0040_agent_preset"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(APIFY_RUNS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS apify_runs")
