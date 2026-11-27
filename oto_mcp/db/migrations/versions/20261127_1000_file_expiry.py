"""L'échéance d'un fichier de projet gardé pour un temps (docs/projects.md §Gros fichiers).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/projects.py::DDL_COLONNE_EXPIRES_AT`, exécuté tel quel : `project_files.expires_at
TIMESTAMPTZ`, plus un index partiel sur les seules lignes qui la portent
(`DDL_INDEX_EXPIRES_AT`). NULL = gardé tant qu'on ne le supprime pas : l'état de tous
les fichiers d'avant. Un gros fichier déposé par parties (`large_uploads`) la porte ;
passée, la boucle `fichiers_echus` retire l'objet et la ligne.

**Verrous.** `AccessExclusiveLock` bref sur `project_files` (colonne nullable sans
défaut : catalogue seul, aucune réécriture) ; l'index partiel ne couvre aucune ligne
existante. `lock_timeout` 5 s : un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose la colonne avant de servir
(`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne. Les échéances posées se perdent : les gros fichiers
deviennent gardés sans limite.

Révision : 20261127_1000_file_expiry
Précédente : 20261125_1103_revocations
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.projects import COLONNE_EXPIRES_AT, DDL_COLONNE_EXPIRES_AT, DDL_INDEX_EXPIRES_AT

revision = "20261127_1000_file_expiry"
down_revision = "20261125_1103_revocations"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_EXPIRES_AT)
    op.execute(DDL_INDEX_EXPIRES_AT)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP INDEX IF EXISTS idx_project_files_expires")
    op.execute(f"ALTER TABLE project_files DROP COLUMN IF EXISTS {COLONNE_EXPIRES_AT}")
