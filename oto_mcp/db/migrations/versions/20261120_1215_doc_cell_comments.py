"""Les commentaires des personnes sur une page de mémoire, et sur une CELLULE ou une
COLONNE de tableau (docs/commentaires-de-procedure.md, docs/datastore.md §Commentaires).

Deux changements, posés aussi par le démarrage (mêmes ordres, ordre indifférent) :

- la table NEUVE `doc_comments` et ses index — le fragment
  `db/schema/projects.py::COMMENTAIRES`, exécuté tel quel : `(id, doc_id → docs ON
  DELETE CASCADE, parent_id → doc_comments, quote, quote_prefix, auteur_sub, body,
  created_at, deleted_at)`, index partiel `(doc_id, id) WHERE deleted_at IS NULL`,
  `(parent_id)` partiel et `(auteur_sub)` (la fusion de comptes repointe la colonne) ;
- la cible élargie de `datastore_row_comments` (`db/datastore_commentaires.py`) : la
  colonne `column_key TEXT` (nullable, sans défaut), `row_id` qui devient nullable, la
  contrainte `datastore_row_comments_cible` (au moins l'un des deux) et l'index partiel
  des commentaires de colonne `(ns_id, column_key, id) WHERE row_id IS NULL AND
  deleted_at IS NULL`.

**Verrous.** La table neuve est vide. Sur `datastore_row_comments` : un
`AccessExclusiveLock` bref par ordre (catalogue seul pour la colonne et le relâchement ;
la contrainte valide les lignes existantes, qui portent toutes un `row_id` — table de
commentaires, petite). `lock_timeout` 5 s : un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes objets avant de
servir. L'ancien code ignore la colonne, écrit toujours `row_id`, et ne lit que par
`row_id = …` : un commentaire de colonne lui est invisible, un commentaire de cellule
lui apparaît comme un commentaire de sa ligne.

Retour arrière : retire l'index, la contrainte et la colonne (les commentaires de
colonne, sans ligne, sont détruits d'abord, puis `row_id` redevient NOT NULL — les
commentaires de cellule restent, comme commentaires de leur ligne), puis la table des
commentaires de page (ils se perdent ; les pages restent entières).

Révision : 20261120_1215_doc_cell_comments
Précédente : 20261119_1600_repar_consentie
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import datastore_commentaires as dc
from oto_mcp.db.schema.projects import COMMENTAIRES

revision = "20261120_1215_doc_cell_comments"
down_revision = "20261119_1600_repar_consentie"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(COMMENTAIRES)
    op.execute(dc.DDL_COLONNE_CIBLE)
    op.execute(dc.DDL_LIGNE_FACULTATIVE)
    op.execute(dc.DDL_CONTRAINTE_CIBLE)
    op.execute(dc.DDL_INDEX_COLONNE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"DROP INDEX IF EXISTS {dc.INDEX_COLONNE}")
    op.execute(f"ALTER TABLE datastore_row_comments DROP CONSTRAINT IF EXISTS "
               f"{dc.CONTRAINTE_CIBLE}")
    op.execute("DELETE FROM datastore_row_comments WHERE row_id IS NULL")
    op.execute("ALTER TABLE datastore_row_comments ALTER COLUMN row_id SET NOT NULL")
    op.execute(f"ALTER TABLE datastore_row_comments DROP COLUMN IF EXISTS {dc.COLONNE_CIBLE}")
    op.execute("DROP TABLE IF EXISTS doc_comments")
