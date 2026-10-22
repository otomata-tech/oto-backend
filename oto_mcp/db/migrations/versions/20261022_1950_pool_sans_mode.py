"""org_model_subscription_modes.mode : NULL = rien CHOISI.

Sans mode réglé, le pool COMPLÈTE l'abonnement de chacun (revue du 19/10/2026,
`org_subscription_pool.sql_par_le_pool`) : un membre dont l'abonnement sert le garde,
le pool sert les autres et les flottes. Prêter ne demande aucun admin ; le pool
exclusif reste un réglage d'admin (`mode = 'pool'`).

Une ligne naissait aussi du repli API (`set_repli_api`), figée à `personnel` : ouvrir
le repli aurait fermé ce défaut sans que personne ne l'ait choisi. Elle naît
désormais SANS mode, ce qu'il faut que la colonne accepte. La CHECK laisse passer
NULL telle quelle.

Les lignes déjà nées du repli à `personnel` le restent : rien ne les distingue d'un
choix d'admin, et les rouvrir déciderait à sa place.

**Ordre avec le code : la révision D'ABORD.** Le code neuf écrit NULL au repli API ;
sur une colonne encore NOT NULL, il lèverait. Geste sur notre box : `oto-mcp migrer
upgrade head` par le lanceur (docs/migrations-versionnees.md §5.1), avant le tag.

**Verrous.** `AccessExclusiveLock` bref sur une petite table (une ligne par org
réglée), aucun parcours : relâcher un NOT NULL ne relit pas les lignes.

Retour arrière : `UPDATE org_model_subscription_modes SET mode = 'personnel' WHERE
mode IS NULL`, puis `ALTER COLUMN mode SET NOT NULL`.

Révision : 20261022_1950_pool_sans_mode
Précédente : 20261021_1352_outils_act_envois
"""
from __future__ import annotations

from alembic import op

revision = "20261022_1950_pool_sans_mode"
down_revision = "20261021_1352_outils_act_envois"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE org_model_subscription_modes ALTER COLUMN mode DROP NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE org_model_subscription_modes SET mode = 'personnel' WHERE mode IS NULL")
    op.execute("ALTER TABLE org_model_subscription_modes ALTER COLUMN mode SET NOT NULL")
