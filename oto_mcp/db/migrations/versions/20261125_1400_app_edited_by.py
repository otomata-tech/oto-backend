"""Apps : qui a écrit l'état vivant et le brouillon (`apps.edited_by`,
`apps.draft_edited_by`), à côté de leur auteur (`updated_by`, `draft_by`).

Des colonnes NEUVES sur une table déjà posée — le DDL est le fragment
`db/schema/apps.py::EDITEUR`, exécuté tel quel (`ADD COLUMN IF NOT EXISTS`) : le
démarrage pose les mêmes colonnes, une base où elles manquent les reçoit sans qu'on les
réécrive.

Nullables, sans défaut : NULL = l'auteur (un état d'avant). Un éditeur qui n'atteint pas
tous les tableaux d'une app la modifie désormais sans en devenir l'auteur, tant qu'il ne
change ni ses tableaux ni ses agents (`docs/apps.md` § Modifier une app sans ses
tableaux) : ces colonnes disent qui a écrit. **Personne ne gagne d'accès** : l'auteur, et
donc ce que l'app peut lire, ne change pas.

**Verrous.** `AccessExclusiveLock` bref sur `apps` (catalogue seul : colonnes nullables
sans défaut, aucune réécriture). `lock_timeout` 5 s : un échec propre se rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose les colonnes avant de servir ;
l'ancien code les ignore.

Retour arrière : retire les colonnes (on ne sait plus qui a écrit un état dont il n'est
pas l'auteur ; l'auteur reste).

Révision : 20261125_1400_app_edited_by
Précédente : 20261125_1103_revocations
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import EDITEUR

revision = "20261125_1400_app_edited_by"
down_revision = "20261125_1103_revocations"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(EDITEUR)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for col in ("edited_by", "draft_edited_by"):
        op.execute(f"ALTER TABLE apps DROP COLUMN IF EXISTS {col}")
