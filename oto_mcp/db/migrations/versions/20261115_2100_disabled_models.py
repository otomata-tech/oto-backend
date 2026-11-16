"""The models an org has turned off for its agents — the org setting
`orgs.disabled_models` (`db/modeles_eteints.py`).

A NEW column, `TEXT[] NOT NULL DEFAULT '{}'` — the DDL is
`db/modeles_eteints.py::DDL_COLONNE`, run as is. Empty = every org before the switch:
every model stays on. Only an org admin turns one off (`oto_org_settings domain=models`).

**Locks.** Brief `AccessExclusiveLock` on `orgs` (constant non-volatile default: catalog
only, no rewrite, PostgreSQL ≥ 11). `lock_timeout` 5 s: `orgs` is read on every request;
a clean failure, replayed.

**Order with the code: indifferent.** The boot adds the column before serving (same DDL,
under a catalog guard); old code ignores it.

Rollback: drops the column (every model is on again).

Révision : 20261115_2100_disabled_models
Précédente : 20261113_2100_spend_blocks
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.modeles_eteints import COLONNE, DDL_COLONNE

revision = "20261115_2100_disabled_models"
down_revision = "20261113_2100_spend_blocks"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs DROP COLUMN IF EXISTS {COLONNE}")
