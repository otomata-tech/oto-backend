"""Les demandes d'effacement d'une org (docs/effacement-d-org.md).

Une table NEUVE, née entière, et ses index — le DDL est le fragment
`db/schema/effacements.py::EFFACEMENTS`, exécuté tel quel (une seule écriture) :
`org_erasures (id, org_id, state, via, reason, requested_by, requested_at, token_hash,
token_expires_at, confirm_sub, confirm_email, org_name, confirmed_at, scheduled_for,
cancelled_by, cancelled_at, started_at, finished_at, attempts, last_error, snapshot,
plan, receipt, commerce_erased_at)`, un index unique partiel « une demande vivante par
org », un index unique partiel sur l'empreinte du jeton, un index partiel d'échéance.

**Verrous.** `AccessExclusiveLock` sur la table neuve seule (vide, instantané) ; aucune
table existante n'est touchée. `lock_timeout` 5 s, comme les autres révisions.

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE
IF NOT EXISTS`, même fragment) ; l'ancien code l'ignore.

Retour arrière : retire la table (les demandes et leurs reçus se perdent ; aucune donnée
d'une org n'en dépend).

Révision : 20261112_1200_org_erasures
Précédente : 20261111_1930_dataforseo_decrit
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.effacements import EFFACEMENTS

revision = "20261112_1200_org_erasures"
down_revision = "20261111_1930_dataforseo_decrit"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(EFFACEMENTS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS org_erasures")
