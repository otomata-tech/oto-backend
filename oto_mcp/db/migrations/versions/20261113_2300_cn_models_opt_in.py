"""Models that run in China become OPT-IN: `orgs.allow_cn_models` defaults to FALSE and
every org is set to FALSE (`db/regions_de_modeles.py`).

Agent turns carry customer data (the prompt, rows read, tool results). Since the same
day the default open models (DeepSeek V4.1 Flash, MiniMax M3) are pinned to US hosts
only, so an org that never opted in still has them; only DeepSeek V4 Pro keeps hosts in
China and is offered once an org admin allows it (`oto_org_settings
domain=model_regions`).

**Why every org, not only new ones.** No org could run these models before this
revision: no OpenRouter worker and no platform key were live, so no admin had a reason
to set the switch and no agent depends on it. Resetting all rows keeps one rule — China
is something an admin chose — instead of TRUE for orgs born before it.

**Locks.** `SET DEFAULT` is catalog only. The `UPDATE` rewrites the org rows that are
TRUE (hundreds): brief row locks. `lock_timeout` 5 s: `orgs` is read on every request; a
clean failure, replayed.

**Order with the code: indifferent.** Old code reads the column the same way; it only
sees fewer orgs allowed.

Rollback: the default goes back to TRUE; the values stay FALSE (who had chosen what
before is not kept, and nobody had).

Révision : 20261113_2300_cn_models_opt_in
Précédente : 20261113_1800_usage_org
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.regions_de_modeles import COLONNE, DDL_DEFAUT

revision = "20261113_2300_cn_models_opt_in"
down_revision = "20261113_1800_usage_org"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_DEFAUT)
    op.execute(f"UPDATE orgs SET {COLONNE} = FALSE WHERE {COLONNE}")


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs ALTER COLUMN {COLONNE} SET DEFAULT TRUE")
