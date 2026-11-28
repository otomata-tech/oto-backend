"""La recette que joue un agent sans modèle.

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/runner_triggers.py::DDL_COLONNE_RECETTE`, exécuté tel quel :
`runner_triggers.recipe JSONB`. `{slug, version, params, datastore}` d'un agent-recette
(`capabilities/_declencheur_recette`), la version épinglée ; NULL = un agent à modèle,
l'état de tous les agents d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `runner_triggers` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick lit la table en
continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait ses lectures.

**Ordre avec le code : indifférent.** Le code de ce lot lit la colonne (`_COLS`), mais le
démarrage la pose avant de servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de
catalogue) ; l'ancien code l'ignore et lit un agent-recette comme un agent sans modèle
(occurrence sautée, `refused_no_model`).

Retour arrière : retire la colonne. Les agents-recettes posés deviennent des agents sans
modèle ni procédure : ils ne tournent plus (aucune occurrence n'est enfilée).

Révision : 20261127_1800_triggers_recette
Précédente : 20261125_1700_responsables
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.runner_triggers import COLONNE_RECETTE, DDL_COLONNE_RECETTE

revision = "20261127_1800_triggers_recette"
down_revision = "20261125_1700_responsables"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_RECETTE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_triggers DROP COLUMN IF EXISTS {COLONNE_RECETTE}")
