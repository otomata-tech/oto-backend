"""Le relevé d'un reveal de téléphone Apollo commandé sur la clé plateforme.

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/apollo_reveals.py::DDL_COLONNE_RELEVE`, exécuté tel quel :
`apollo_phone_reveals.releve_le TIMESTAMPTZ`. Quand le coût de la commande a été tracé
pour le facturant, par la relève qui la lit ou par le relevé serveur
(`apollo_releve.py`) qui facture celles que personne n'a relues. NULL = pas encore — et
toujours NULL sur une clé propre, que le client paie lui-même.

**Verrous.** `AccessExclusiveLock` bref sur `apollo_phone_reveals` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : un échec net, qu'on
rejoue, plutôt qu'une attente qui bloquerait le receveur derrière elle.

**Ordre avec le code : indifférent.** Le démarrage la pose avant de servir (`ADD COLUMN
IF NOT EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (le relevé serveur refacturerait les commandes déjà
relues : le facturant n'en garde qu'une ligne par commande).

Révision : 20261126_1200_apollo_releve
Précédente : 20261125_1103_revocations
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.apollo_reveals import COLONNE_RELEVE, DDL_COLONNE_RELEVE

revision = "20261126_1200_apollo_releve"
down_revision = "20261125_1103_revocations"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_RELEVE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE apollo_phone_reveals DROP COLUMN IF EXISTS {COLONNE_RELEVE}")
