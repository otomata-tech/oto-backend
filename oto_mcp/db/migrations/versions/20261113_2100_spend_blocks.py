"""Limites de dépense du jour : la personne d'une réservation, et les blocages poussés.

Deux DDL, tous deux posés aussi par le démarrage (même texte,
`db/schema/credits.py`) :

- `credit_holds.acting_sub TEXT` (`ACTEUR_DES_RESERVATIONS`) : une colonne NEUVE,
  nullable, sans défaut — la personne qui a agi pour la réservation. NULL = toute
  réservation d'avant, ce qui est vrai : on ne le savait pas.
- `spend_blocks` (`SPEND_BLOCKS`) : une table NEUVE, née vide. FK vers `orgs`, ON DELETE
  CASCADE : les blocages partent avec l'org.

**Verrous.** `AccessExclusiveLock` bref sur `credit_holds` (colonne nullable sans
défaut : catalogue seul, aucune réécriture) ; la FK de la table neuve prend un
`ShareRowExclusiveLock` bref sur `orgs`. `lock_timeout` 5 s : chaque appel payant
réserve dans `credit_holds` ; un échec net, qu'on rejoue, plutôt qu'une attente qui
bloquerait les réservations derrière elle.

**Ordre avec le code : indifférent.** Le démarrage pose les deux avant de servir ;
l'ancien code les ignore.

Retour arrière : retire la table (les blocages en cours sont levés) et la colonne (les
personnes notées se perdent ; le commerce ne peut plus attribuer les tours réglés).

Révision : 20261113_2100_spend_blocks
Précédente : 20261113_1800_usage_org
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.credits import ACTEUR_DES_RESERVATIONS, SPEND_BLOCKS

revision = "20261113_2100_spend_blocks"
down_revision = "20261113_1800_usage_org"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(ACTEUR_DES_RESERVATIONS)
    op.execute(SPEND_BLOCKS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS spend_blocks")
    op.execute("ALTER TABLE credit_holds DROP COLUMN IF EXISTS acting_sub")
