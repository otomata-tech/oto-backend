"""La bibliothèque d'apps de la communauté : l'instantané publié d'une app.

Une table NEUVE, née entière (`db/schema/apps.py::APP_LIBRARY`) — posée aussi par le
démarrage, même DDL. FK vers `orgs` seulement (`author_org_id`, ON DELETE SET NULL).

**Verrous.** Table neuve et vide ; la FK prend un `ShareRowExclusiveLock` bref sur
`orgs`. `lock_timeout` 5 s.

**Ordre avec le code : indifférent.** L'ancien code l'ignore.

Retour arrière : retire la table (les apps publiées se perdent ; les apps installées
restent, ce sont des copies).

Révision : 20261125_1300_app_library
Précédente : 20261125_1200_communaute
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import APP_LIBRARY

revision = "20261125_1300_app_library"
down_revision = "20261125_1200_communaute"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(APP_LIBRARY)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS app_library")
