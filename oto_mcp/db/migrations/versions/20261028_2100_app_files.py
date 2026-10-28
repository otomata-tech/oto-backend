"""Apps : les fichiers qu'un lecteur dépose dans une app pour ses agents (`app_files`).

Une table NEUVE, née entière — le DDL est le fragment `db/schema/apps.py::FICHIERS`,
exécuté tel quel. Les octets vont sur le stockage média sous `app-files/`.

**Verrous.** `AccessExclusiveLock` sur la table neuve (vide) ; la clé étrangère vers
`apps` prend un `ShareRowExclusiveLock` bref. `lock_timeout` 5 s.

**Ordre avec le code : indifférent.** Le démarrage pose la même table ; l'ancien code
l'ignore.

Retour arrière : retire la table (les objets `app-files/` restent sur le stockage).

⚠️ Tête de chaîne : oto-backend#1420 (pièces jointes du copilote) chaîne aussi sur
`20261027_1800_row_comments` — celle des deux qui arrive en second rechaîne son
`down_revision`, sinon `test_une_seule_tete` rougit.

Révision : 20261028_2100_app_files
Précédente : 20261028_1900_apps_owned
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import FICHIERS

revision = "20261028_2100_app_files"
down_revision = "20261028_1900_apps_owned"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(FICHIERS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS app_files")
