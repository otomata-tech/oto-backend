"""Le repli d'un abonnement épuisé sur la clé de PLATEFORME : le réglage de l'org
(`org_model_subscription_modes.repli_cle_plateforme`, qui l'a posé et quand).

Des colonnes NEUVES sur une table déjà posée — le DDL est le fragment
`db/schema/runs.py::MODEL_SUBSCRIPTION_REPLI_PLATEFORME`, exécuté tel quel
(`ADD COLUMN IF NOT EXISTS`) : le démarrage pose les mêmes colonnes, une base où elles
manquent les reçoit sans qu'on les réécrive.

`repli_cle_plateforme BOOLEAN NOT NULL DEFAULT FALSE` : toute org d'avant n'a rien
choisi, et rien ne change pour elle (constante : PostgreSQL ≥ 11 ne réécrit pas la
table). `repli_cle_plateforme_by` (une colonne de compte, repointée par `migrate_sub`)
et `repli_cle_plateforme_at` sont nullables, sans défaut.

**Verrous.** `AccessExclusiveLock` bref sur `org_model_subscription_modes` (catalogue
seul). La réservation des travaux d'abonnement la lit à chaque sondage : `lock_timeout`
5 s, un échec net qu'on rejoue plutôt qu'une attente qui bloquerait la file.

**Ordre avec le code : indifférent.** Le démarrage pose les colonnes avant de servir ;
l'ancien code les ignore. Le repli sur la clé de plateforme reste de toute façon fermé
tant que l'instance ne le permet pas (`OTO_REPLI_CLE_PLATEFORME_PERMIS`).

Retour arrière : retire les colonnes (le réglage des orgs qui l'avaient ouvert se perd ;
leurs travaux au plafond sans clé d'org attendent de nouveau la réinitialisation).

Révision : 20261119_1400_repli_plateforme
Précédente : 20261119_1300_app_use_role
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.runs import MODEL_SUBSCRIPTION_REPLI_PLATEFORME

revision = "20261119_1400_repli_plateforme"
down_revision = "20261119_1300_app_use_role"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
_COLONNES = ("repli_cle_plateforme", "repli_cle_plateforme_by", "repli_cle_plateforme_at")


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(MODEL_SUBSCRIPTION_REPLI_PLATEFORME)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for col in _COLONNES:
        op.execute(f"ALTER TABLE org_model_subscription_modes DROP COLUMN IF EXISTS {col}")
