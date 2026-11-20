"""Les passages de l'archive du journal : une ligne par passage, réussi ou en échec.

Une table NEUVE, née entière : `journal_archive_passages`
(`db/schema/usage.py::JOURNAL_ARCHIVE_PASSAGES`) — posée aussi par le démarrage
(`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL. Aucune FK, aucune colonne de
compte. Écrite par `deploy/archive_tool_calls.py`, lue par `oto_admin_monitoring
op=journal_archive`.

**Verrous.** Aucun sur une table existante.

**Ordre avec le code : indifférent pour le backend** (le démarrage pose la même table,
l'ancien code l'ignore). ⚠️ Le script d'archive du tag qui porte cette révision écrit
dans la table à chaque passage : elle doit exister avant son premier tir (démarrage du
backend ou cette révision).

Retour arrière : retire la table (l'historique des passages se perd ; rien d'autre).

Révision : 20261120_2220_archive_passages
Précédente : 20261120_2210_tableau_par_nom
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.usage import JOURNAL_ARCHIVE_PASSAGES

revision = "20261120_2220_archive_passages"
down_revision = "20261120_2210_tableau_par_nom"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(JOURNAL_ARCHIVE_PASSAGES)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS journal_archive_passages")
