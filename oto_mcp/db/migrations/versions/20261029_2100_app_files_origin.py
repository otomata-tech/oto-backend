"""Apps : la provenance d'un fichier d'app (`app_files.origin`) — déposé par le lecteur
(`upload`) ou rendu par un agent qu'il a lancé (`agent`, `oto_apps op=put_file`).

Une colonne NEUVE sur une table déjà posée par `20261029_2000_app_files` — le DDL est le
fragment `db/schema/apps.py::FICHIERS_ORIGINE`, exécuté tel quel (`ADD COLUMN IF NOT
EXISTS`) : le démarrage pose la même colonne, une base où le fragment `FICHIERS` a déjà
été joué la reçoit sans qu'on le réécrive.

`NOT NULL DEFAULT 'upload'` : les fichiers d'avant sont tous des dépôts du lecteur, ce
que le défaut dit (constante : PostgreSQL ≥ 11 ne réécrit pas la table).

Elle aligne aussi la clé étrangère `app_files.app_id` sur le fragment d'aujourd'hui
(`ON DELETE SET NULL`, colonne nullable : une app supprimée laisse ses lignes à la purge
de maintenance, qui retire leurs objets — jamais un objet orphelin) là où une version
antérieure du fragment l'avait posée en `CASCADE`. Idempotent : sur une base née du
fragment d'aujourd'hui, la contrainte est simplement reposée à l'identique.

**Verrous.** `AccessExclusiveLock` bref sur `app_files` (catalogue seul pour la colonne ;
la contrainte se revalide sur une table bornée par la purge de 30 jours) et
`ShareRowExclusiveLock` bref sur `apps` le temps de poser la clé étrangère.
`lock_timeout` 5 s. Hors du démarrage : le boot ne rejoue que `FICHIERS_ORIGINE`.

**Ordre avec le code : indifférent.** Le démarrage pose la colonne avant de servir ;
l'ancien code l'ignore.

Retour arrière : retire la colonne (un fichier rendu par un agent se lit alors comme un
dépôt).

Révision : 20261029_2100_app_files_origin
Précédente : 20261029_2000_app_files
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import FICHIERS_ORIGINE

revision = "20261029_2100_app_files_origin"
down_revision = "20261029_2000_app_files"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(FICHIERS_ORIGINE)
    op.execute("ALTER TABLE app_files ALTER COLUMN app_id DROP NOT NULL")
    op.execute("ALTER TABLE app_files DROP CONSTRAINT IF EXISTS app_files_app_id_fkey")
    op.execute("ALTER TABLE app_files ADD CONSTRAINT app_files_app_id_fkey "
               "FOREIGN KEY (app_id) REFERENCES apps(id) ON DELETE SET NULL")


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE app_files DROP COLUMN IF EXISTS origin")
