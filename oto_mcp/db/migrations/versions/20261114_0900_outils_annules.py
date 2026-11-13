"""Les outils qu'une personne a retirés après le médecin des agents
(`runner_triggers.repair_undone_tools`).

Une colonne NEUVE, nullable, sans défaut, sur `runner_triggers` — posée aussi par le
démarrage, sous garde de catalogue, même DDL (`db/runner_triggers.py::
DDL_COLONNE_OUTILS_ANNULES`) : la liste des outils dont quelqu'un qui peut modifier
l'agent a défait l'ajout (`POST /api/orgs/{id}/agent-health/undo`). Le médecin ne les
redonne plus à cet agent (`add_tool` → 409 `repair_undone`).

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit cette table en
continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui le bloquerait.

**Ordre avec le code : indifférent.** Le démarrage pose la même colonne ; l'ancien code
l'ignore.

Retour arrière : retire la colonne (les retraits se perdent : le médecin pourrait de
nouveau donner ces outils ; les épisodes, qui journalisent l'annulation, restent).

Révision : 20261114_0900_outils_annules
Précédente : 20261113_1900_sante_agents
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import runner_triggers

revision = "20261114_0900_outils_annules"
down_revision = "20261113_1900_sante_agents"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(runner_triggers.DDL_COLONNE_OUTILS_ANNULES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS "
               f"{runner_triggers.COLONNE_OUTILS_ANNULES}")
