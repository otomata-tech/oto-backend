"""Le dossier d'affichage d'un projet (docs/projects.md §Dossier d'affichage).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/projects.py::DDL_COLONNE_FOLDER`, exécuté tel quel : `projects.folder TEXT`. Un
libellé libre que les listes regroupent ; NULL = hors dossier, l'état de tous les
projets d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `projects` (colonne nullable sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : `projects` est lue à chaque
requête qui touche un projet ; un échec net, qu'on rejoue, plutôt qu'une attente qui
bloquerait ces lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne
(`_PROJECT_COLS`), mais le démarrage la pose avant de servir (`ADD COLUMN IF NOT
EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (les dossiers posés se perdent, les projets
reviennent hors dossier).

Révision : 0062_projects_folder
Précédente : 0061_valeurs_materialisees
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.projects import COLONNE_FOLDER, DDL_COLONNE_FOLDER

revision = "0062_projects_folder"
down_revision = "0061_valeurs_materialisees"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_FOLDER)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE projects DROP COLUMN IF EXISTS {COLONNE_FOLDER}")
