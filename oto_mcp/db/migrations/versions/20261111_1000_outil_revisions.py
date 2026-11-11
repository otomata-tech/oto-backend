"""Qui a TROUVÉ la valeur : `outil` et `fournisseur` sur `datastore_row_revisions`.

Colonnes NEUVES, nullables, sans défaut — posées aussi par le démarrage, sous garde de
catalogue, même DDL (`db/journal_revisions.py::DDL_COLONNES_ATTRIBUTION`). Le
déclencheur du journal les remplit depuis les réglages `oto.outil` / `oto.fournisseur`
(`db/estampille.py`) ; elles servent l'en-tête d'une colonne (« trouvé par Jev, Hunter »,
`db/historique.sources_des_colonnes`).

**Verrous.** `AccessExclusiveLock` bref sur le journal (catalogue seul, aucune
réécriture). `lock_timeout` 5 s : chaque écriture de ligne alimente ce journal ; un
échec net, qu'on rejoue, plutôt qu'une attente qui bloquerait les écritures.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes colonnes ; l'ancien
corps de la fonction ne les nomme pas, elles restent `NULL`.

Retour arrière : retire les colonnes (l'attribution des révisions écrites depuis se
perd ; les révisions restent). Repose d'abord une version qui ne les écrit pas.

Révision : 20261111_1000_outil_revisions
Précédente : 20261109_2120_alertes_slack
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import journal_revisions

revision = "20261111_1000_outil_revisions"
down_revision = "20261109_2120_alertes_slack"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for ddl in journal_revisions.DDL_COLONNES_ATTRIBUTION:
        op.execute(ddl)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for col in journal_revisions.COLONNES_ATTRIBUTION:
        op.execute(f"ALTER TABLE {journal_revisions.TABLE} DROP COLUMN IF EXISTS {col}")
