"""Les commentaires des personnes sur une procédure (docs/commentaires-de-procedure.md).

Une table NEUVE, née entière, et ses index — le DDL est le fragment
`db/schema/procedures.py::COMMENTAIRES`, exécuté tel quel (une seule écriture) :
`procedure_comments (id, instruction_id, parent_id → procedure_comments, version,
quote, quote_prefix, auteur_sub, body, created_at, deleted_at)`, index partiel
`(instruction_id, id) WHERE deleted_at IS NULL`, `(parent_id)` partiel et `(auteur_sub)`
(la fusion de comptes repointe cette colonne). Vers la procédure, lien logique sans clé
étrangère (`org_instructions.id`, comme `process_shares`).

**Verrous.** `AccessExclusiveLock` sur la table neuve seule (vide, instantané) ; aucune
table existante n'est touchée. `lock_timeout` 5 s, comme les autres révisions.

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE
IF NOT EXISTS`, même fragment) ; l'ancien code l'ignore.

Retour arrière : retire la table (les commentaires se perdent ; les procédures et leurs
versions restent entières).

Révision : 20261108_1200_procedure_comments
Précédente : 20261105_1700_orgs_auto_created
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.procedures import COMMENTAIRES

revision = "20261108_1200_procedure_comments"
down_revision = "20261105_1700_orgs_auto_created"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(COMMENTAIRES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS procedure_comments")
