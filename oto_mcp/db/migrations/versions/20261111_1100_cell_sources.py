"""`datastore_cell_sources` + son déclencheur : qui a TROUVÉ la valeur de chaque case.

Table NEUVE, née vide, et trois déclencheurs `AFTER` sur `datastore_rows` qui
l'entretiennent (`db/attribution_cellules.py`). Posés aussi par le démarrage, sous
garde, même DDL.

**Verrous.** `CREATE TRIGGER` prend un `SHARE ROW EXCLUSIVE` bref sur `datastore_rows`
(bloque les écritures de lignes le temps du catalogue, pas les lectures).
`lock_timeout` 5 s : un échec net, qu'on rejoue, plutôt qu'une file d'écritures.

**Ordre avec le code : indifférent.** Sans l'estampille `oto.outil`/`oto.fournisseur`
(ancien code), aucune écriture n'est attribuée : la fonction ne fait que retirer.

Retour arrière : retire déclencheurs, fonction et table (l'attribution se perd).

Révision : 20261111_1100_cell_sources
Précédente : 20261111_1000_outil_revisions
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import attribution_cellules as ac

revision = "20261111_1100_cell_sources"
down_revision = "20261111_1000_outil_revisions"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(ac.DDL_TABLE)
    op.execute(ac.FONCTION)
    for nom, quand, condition in ac._DECLENCHEURS:
        # Sous garde de catalogue, jamais `DROP … IF EXISTS` d'abord : un `DROP
        # TRIGGER` prend `ACCESS EXCLUSIVE` (bloque aussi les lectures), même absent.
        op.execute(ac.ddl_declencheur_si_absent(nom, quand, condition))


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for nom, _, _ in ac._DECLENCHEURS:
        op.execute(f"DROP TRIGGER IF EXISTS {nom} ON datastore_rows")
    op.execute(f"DROP FUNCTION IF EXISTS {ac.NOM_FONCTION}()")
    op.execute(f"DROP TABLE IF EXISTS {ac.TABLE}")
