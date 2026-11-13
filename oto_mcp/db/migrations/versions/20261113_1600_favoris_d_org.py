"""Les favoris d'une org : pages et tableaux épinglés pour tous ses membres, ou pour soi.

Une table NEUVE, née entière : `org_favorites` (`db/schema/orgs.py::ORG_FAVORITES`) —
posée aussi par le démarrage (`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL.
FK vers `orgs` et vers `users` (`owner_sub`, favori personnel), ON DELETE CASCADE ;
aucune FK vers la page ou le tableau épinglé.

**Verrous.** La table est neuve et vide ; ses FK prennent un `ShareRowExclusiveLock` bref
sur `orgs` et `users`. `lock_timeout` 5 s : un échec net, qu'on rejoue, plutôt qu'une attente.

**Ordre avec le code : indifférent.** Le démarrage pose la même table ; l'ancien code
l'ignore.

Retour arrière : retire la table (les favoris se perdent ; rien d'autre ne les lit).

Révision : 20261113_1600_favoris_d_org
Précédente : 20261113_1200_tours_au_cout_cle
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.orgs import ORG_FAVORITES

revision = "20261113_1600_favoris_d_org"
down_revision = "20261113_1200_tours_au_cout_cle"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(ORG_FAVORITES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS org_favorites")
