"""Where an org lets its agents' models run — the org setting `orgs.allow_cn_models`
(`db/regions_de_modeles.py`).

A NEW column, `BOOLEAN NOT NULL DEFAULT TRUE` — the DDL is
`db/regions_de_modeles.py::DDL_COLONNE`, run as is. ⚠️ That DDL now says DEFAULT FALSE
(13/11/2026): a base that replays this revision gets FALSE at once, and
`20261113_2300_cn_models_opt_in` moves the others there. TRUE = every org before the switch:
models that run in China stay allowed. Only an org admin turns it off
(`oto_org_settings domain=model_regions`).

**Locks.** Brief `AccessExclusiveLock` on `orgs` (constant non-volatile default: catalog
only, no rewrite, PostgreSQL ≥ 11). `lock_timeout` 5 s: `orgs` is read on every request;
a clean failure, replayed.

**Order with the code: indifferent.** The boot adds the column before serving (same DDL,
under a catalog guard); old code ignores it.

Rollback: drops the column (orgs that had turned it off are allowed again).

Révision : 20261112_1900_allow_cn_models
Précédente : 20261112_1700_app_drafts
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.regions_de_modeles import COLONNE, DDL_COLONNE

revision = "20261112_1900_allow_cn_models"
down_revision = "20261112_1700_app_drafts"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs DROP COLUMN IF EXISTS {COLONNE}")
