"""Apps : les fichiers qu'un lecteur dépose dans une app pour ses agents (`app_files`).

Une table NEUVE, née entière — le DDL est le fragment `db/schema/apps.py::FICHIERS`,
exécuté tel quel. Les octets vont sur le stockage média sous `app-files/`.

**Verrous.** `AccessExclusiveLock` sur la table neuve (vide) ; la clé étrangère vers
`apps` prend un `ShareRowExclusiveLock` bref. `lock_timeout` 5 s.

**Ordre avec le code : indifférent.** Le démarrage pose la même table ; l'ancien code
l'ignore.

**Supprimer une app ne laisse pas d'objet orphelin** : `app_id` est `ON DELETE SET
NULL`, pas `CASCADE` — la ligne reste, sans app, et la purge de maintenance
(`oto-mcp maintenance app-files`) retire son objet puis la ligne au passage suivant,
sans attendre les 30 jours. Une ligne sans app n'est lue par personne (toutes les
lectures portent `app_id = …`).

Retour arrière : retire la table (les objets `app-files/` restent sur le stockage —
les purger d'abord : `oto-mcp maintenance app-files`).

Révision : 20261029_2000_app_files
Précédente : 20261029_1900_apps_owned
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import FICHIERS

revision = "20261029_2000_app_files"
down_revision = "20261029_1900_apps_owned"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(FICHIERS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS app_files")
