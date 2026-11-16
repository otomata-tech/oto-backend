"""Apps : brouillon et versions publiées (`apps.drafts_enabled`, `published_at`,
`version_note`, `draft_*` ; `app_revisions.published_at`, `note`, `saved_at`).

Des colonnes NEUVES sur des tables déjà posées — le DDL est le fragment
`db/schema/apps.py::VERSIONS`, exécuté tel quel (`ADD COLUMN IF NOT EXISTS`) : le
démarrage pose les mêmes colonnes, une base où elles manquent les reçoit sans qu'on les
réécrive.

`drafts_enabled NOT NULL DEFAULT FALSE` : une app d'avant reste publiée à chaque
écriture, ce que le défaut dit (constante : PostgreSQL ≥ 11 ne réécrit pas la table).
Les autres colonnes sont nullables, sans défaut.

**Verrous.** `AccessExclusiveLock` bref sur `apps` et `app_revisions` (catalogue seul).
`lock_timeout` 5 s.

**Ordre avec le code : indifférent.** Le démarrage pose les colonnes avant de servir ;
l'ancien code les ignore (il écrit l'état vivant en direct, et purge les révisions sans
distinguer les versions publiées — ne pas activer de brouillon avant que la prod tourne
le tag qui les lit).

Retour arrière : retire les colonnes (les brouillons en cours sont perdus, les versions
publiées redeviennent des révisions ordinaires).

Révision : 20261112_1700_app_drafts
Précédente : 20261112_1200_connecteur_api
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import VERSIONS

revision = "20261112_1700_app_drafts"
down_revision = "20261112_1200_connecteur_api"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(VERSIONS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for col in ("drafts_enabled", "published_at", "version_note", "draft_manifest",
                "draft_html", "draft_by", "draft_at"):
        op.execute(f"ALTER TABLE apps DROP COLUMN IF EXISTS {col}")
    for col in ("published_at", "note", "saved_at"):
        op.execute(f"ALTER TABLE app_revisions DROP COLUMN IF EXISTS {col}")
