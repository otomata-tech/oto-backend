"""Le registre des images CONNUES d'une app (`images_connues`).

Une table NEUVE, née entière : `db/schema/apps.py::IMAGES_CONNUES` — posée aussi par le
démarrage (`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL. Aucune FK. Écrite
par les écritures d'app et de ligne qui apprennent une adresse d'image, lue et conclue
par la boucle `images_connues` (`images_connues_worker.py`).

**Verrous.** Aucun sur une table existante.

**Ordre avec le code : indifférent.** Le code de ce lot écrit la table sous un point
de sauvegarde (un échec y est journalisé, jamais propagé) ; l'ancien code l'ignore.

Retour arrière : retire la table. Les copies déjà déposées restent dans le stockage ;
les adresses apprises se perdent (une app n'affiche plus que ce qui sera réappris).

Révision : 20261126_1500_images_connues
Précédente : 20261125_1103_revocations
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import IMAGES_CONNUES

revision = "20261126_1500_images_connues"
down_revision = "20261125_1103_revocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(IMAGES_CONNUES)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS images_connues")
