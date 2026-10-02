"""recipes, recipe_versions : les recettes connecteur → tableau.

Tables NEUVES — le fragment `db/schema/recipes.py::RECIPES`, exécuté tel quel. Une
recette décrit comment les données d'un outil de connecteur arrivent dans un tableau
sans modèle (`oto_recipe`, `docs/recettes.md`) ; ses versions sont immuables.

Aucun `ALTER`, aucune reprise. Clé étrangère seulement de `recipe_versions` vers
`recipes` (cascade), comme les fonctions.

**Ordre** : le démarrage crée les mêmes tables s'il ne les trouve pas (`CREATE TABLE IF
NOT EXISTS`, ADR 0065) — révision et boot sont idempotents l'un envers l'autre. Le code
du lot les LIT et les ÉCRIT dès qu'on appelle `oto_recipe` : la jouer **avant la
fusion**. L'ancien code ne les connaît pas.

⚠️ Prod et préprod partagent la MÊME base. Le retour arrière retire les deux tables et
toutes les recettes écrites ; tant que le fragment reste dans l'assemblage, le
démarrage suivant les recrée vides.

Révision : 0033_recettes
Précédente : 0032_verrou_org_delegation
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.recipes import RECIPES

revision = "0033_recettes"
down_revision = "0032_verrou_org_delegation"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(RECIPES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS recipe_versions")
    op.execute("DROP TABLE IF EXISTS recipes")
