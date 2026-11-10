"""La dernière réparation du médecin des agents, dite au client (`runner_triggers.repair_note`).

Une colonne NEUVE, nullable, sans défaut, sur `runner_triggers` — posée aussi par le
démarrage, sous garde de catalogue, même DDL (`db/runner_triggers.py::
DDL_COLONNE_REPARATION`) : `{at, what}`, quand et ce qui a été changé, en une phrase pour le
client ; servie `repair` sur l'agent pendant sept jours.

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit cette table en
continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui le bloquerait.

**Ordre avec le code : indifférent.** Le démarrage pose la même colonne ; l'ancien code
l'ignore.

Retour arrière : retire la colonne (les réparations dites se perdent ; les épisodes, qui
les journalisent, restent).

Révision : 20261109_2110_note_de_reparation
Précédente : 20261109_2105_issue_des_travaux
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import runner_triggers

revision = "20261109_2110_note_de_reparation"
down_revision = "20261109_2105_issue_des_travaux"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(runner_triggers.DDL_COLONNE_REPARATION)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS "
               f"{runner_triggers.COLONNE_REPARATION}")
