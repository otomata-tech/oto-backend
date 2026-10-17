"""Les minutes du temps gagné qu'une org substitue aux défauts (docs/runner-et-automatisations.md
§Temps gagné).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/runner_jobs.py::DDL_COLONNE_TEMPS_GAGNE`, exécuté tel quel : `orgs.time_saved_minutes
JSONB`. NULL = les défauts de `time_saved.py`, l'état de toutes les orgs d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `orgs` (colonne nullable sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : `orgs` est lue à chaque requête ;
un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ces lectures.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne, mais le démarrage
la pose avant de servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de catalogue) ;
l'ancien code l'ignore.

Retour arrière : retire la colonne (les surcharges posées se perdent, les défauts reviennent).

Révision : 0064_orgs_time_saved_minutes
Précédente : 0063_runner_triggers_folder
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_jobs import COLONNE_TEMPS_GAGNE, DDL_COLONNE_TEMPS_GAGNE

revision = "0064_orgs_time_saved_minutes"
down_revision = "0063_runner_triggers_folder"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_TEMPS_GAGNE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs DROP COLUMN IF EXISTS {COLONNE_TEMPS_GAGNE}")
