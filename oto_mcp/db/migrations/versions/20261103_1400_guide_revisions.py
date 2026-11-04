"""Les versions gardées des guides et des readmes (`db/guide_revisions.py`).

Une table NEUVE, née entière, et deux index — le DDL est le fragment
`db/schema/guides.py::GUIDE_REVISIONS`, exécuté tel quel.

**Verrous.** `AccessExclusiveLock` sur la table neuve seulement (vide, instantané),
aucune clé étrangère. `lock_timeout` 5 s par habitude de la maison.

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE IF
NOT EXISTS`, même fragment) ; l'ancien code l'ignore. Le nouveau y écrit dans la
transaction de chaque écriture de guide : sans la table, l'écriture échouerait — d'où
le démarrage qui la pose avant de servir.

Retour arrière : retire la table (les versions gardées sont perdues ; les guides, eux,
ne bougent pas).

Révision : 20261103_1400_guide_revisions
Précédente : 20261103_1000_copilot_slack
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.guides import GUIDE_REVISIONS

revision = "20261103_1400_guide_revisions"
down_revision = "20261103_1000_copilot_slack"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(GUIDE_REVISIONS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS guide_revisions")
