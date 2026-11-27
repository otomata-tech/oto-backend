"""Les suggestions des commentaires de procédure et de page
(docs/commentaires-de-procedure.md §Suggestions).

Quatre colonnes NEUVES, nullables, sans défaut, sur `procedure_comments` ET sur
`doc_comments` — le DDL est `db/suggestions_commentaires.py`, exécuté tel quel :
`suggestion TEXT` (le texte proposé à la place du passage), `resolution TEXT`
(`accepted` | `rejected`), `resolved_by TEXT`, `resolved_at TIMESTAMPTZ`. NULL partout =
un commentaire ordinaire : l'état de toutes les lignes d'avant, rien n'est recopié.

**Verrous.** Un `AccessExclusiveLock` bref par table (colonnes nullables sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le code de ce lot lit les colonnes, mais le
démarrage les pose avant de servir (`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de
catalogue) ; l'ancien code les ignore — une suggestion lui apparaît comme le commentaire
qu'elle porte.

Retour arrière : retire les quatre colonnes des deux tables (les suggestions deviennent
des commentaires ordinaires ; leur texte proposé et leur issue se perdent).

Révision : 20261126_1200_suggestions
Précédente : 20261125_1710_doc_cell_comments
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.suggestions_commentaires import COLONNES, DDL_PAGES, DDL_PROCEDURES, TABLES

revision = "20261126_1200_suggestions"
down_revision = "20261125_1710_doc_cell_comments"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_PROCEDURES)
    op.execute(DDL_PAGES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} "
                   + ", ".join(f"DROP COLUMN IF EXISTS {c}" for c in COLONNES))
