"""QUAND un abonnement se replie : le réglage de l'org
(`org_model_subscription_modes.repli_quand`, qui l'a posé et quand).

Des colonnes NEUVES sur une table déjà posée — le DDL est le fragment
`db/schema/runs.py::MODEL_SUBSCRIPTION_REPLI_QUAND`, exécuté tel quel
(`ADD COLUMN IF NOT EXISTS`) : le démarrage pose les mêmes colonnes, une base où elles
manquent les reçoit sans qu'on les réécrive.

`repli_quand TEXT NOT NULL DEFAULT 'plafond'` (`plafond` | `plafond_ou_deconnexion`) :
toute org d'avant n'a rien choisi et lit `plafond`, la règle d'avant à l'identique
(constante : PostgreSQL ≥ 11 ne réécrit pas la table ; le CHECK se vérifie sur la
constante, la table a une ligne par org et par famille). `repli_quand_by` (une colonne de
compte, repointée par `migrate_sub`) et `repli_quand_at` sont nullables, sans défaut.

**Verrous.** `AccessExclusiveLock` bref sur `org_model_subscription_modes`. La
réservation des travaux d'abonnement la lit à chaque sondage : `lock_timeout` 5 s, un
échec net qu'on rejoue plutôt qu'une attente qui bloquerait la file.

**Ordre avec le code : indifférent.** Le démarrage pose les colonnes avant de servir ;
l'ancien code les ignore, et `plafond` est ce qu'il fait déjà.

Retour arrière : retire les colonnes (le réglage des orgs qui avaient choisi
`plafond_ou_deconnexion` se perd ; leurs travaux à reconnecter attendent de nouveau).

Révision : 20261122_1200_repli_quand
Précédente : 20261122_1100_repli_plateforme
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.runs import MODEL_SUBSCRIPTION_REPLI_QUAND

revision = "20261122_1200_repli_quand"
down_revision = "20261122_1100_repli_plateforme"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
_COLONNES = ("repli_quand", "repli_quand_by", "repli_quand_at")


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(MODEL_SUBSCRIPTION_REPLI_QUAND)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for col in _COLONNES:
        op.execute(f"ALTER TABLE org_model_subscription_modes DROP COLUMN IF EXISTS {col}")
