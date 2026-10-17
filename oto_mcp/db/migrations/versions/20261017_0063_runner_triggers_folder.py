"""Le dossier d'affichage d'un agent (docs/runner-et-automatisations.md §Dossier d'affichage).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/runner_triggers.py::DDL_COLONNE_FOLDER`, exécuté tel quel :
`runner_triggers.folder TEXT`. Un libellé libre que les listes regroupent ; NULL =
hors dossier, l'état de tous les agents d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit la table
en continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ses
lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne (`_COLS`),
mais le démarrage la pose avant de servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous
garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (les dossiers posés se perdent, les agents
reviennent hors dossier).

Révision : 0063_runner_triggers_folder
Précédente : 0062_outils_act
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_triggers import COLONNE_FOLDER, DDL_COLONNE_FOLDER

revision = "0063_runner_triggers_folder"
down_revision = "0062_outils_act"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_FOLDER)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS {COLONNE_FOLDER}")
