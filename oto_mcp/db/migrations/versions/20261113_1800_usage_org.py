"""Le compteur du jour d'une org sur une clé plateforme dont le quota compte par org.

Une table NEUVE, née entière : `usage_org` (`db/schema/usage.py::USAGE_ORG`) — posée
aussi par le démarrage (`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL. FK vers
`orgs`, ON DELETE CASCADE : le compteur part avec l'org. Aucune colonne de compte.

**Verrous.** La table est neuve et vide ; sa FK prend un `ShareRowExclusiveLock` bref sur
`orgs`. `lock_timeout` 5 s : un échec net, qu'on rejoue, plutôt qu'une attente.

**Ordre avec le code : indifférent.** Le démarrage pose la même table ; l'ancien code
l'ignore.

Retour arrière : retire la table (les compteurs du jour se perdent ; le quota par org
repart de zéro pour la journée).

Révision : 20261113_1800_usage_org
Précédente : 20261113_1600_favoris_d_org
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.usage import USAGE_ORG

revision = "20261113_1800_usage_org"
down_revision = "20261113_1600_favoris_d_org"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(USAGE_ORG)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS usage_org")
