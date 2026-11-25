"""Apps : une app qui prête l'accès d'une personne nommée (`apps.acts_as`,
`acts_as_manifest`, `acts_as_at`) et le réglage de l'org (`orgs.apps_act_as`).

Des colonnes NEUVES sur des tables déjà posées — le DDL est le fragment
`db/schema/apps.py::PRET`, exécuté tel quel (`ADD COLUMN IF NOT EXISTS`) : le démarrage
pose les mêmes colonnes, une base où elles manquent les reçoit sans qu'on les réécrive.

Toutes nullables, sans défaut : une app d'avant ne prête rien (`acts_as` NULL), une org
d'avant suit le défaut de l'instance (`orgs.apps_act_as` NULL), éteint tant que
l'instance ne déclare pas `OTO_APPS_ACT_AS_DEFAULT`. **Personne ne gagne d'accès au
déploiement** : il faut qu'une org le permette ET qu'une personne prête ses droits à une
app.

**Verrous.** `AccessExclusiveLock` bref sur `apps` et `orgs` (catalogue seul : colonnes
nullables sans défaut, aucune réécriture). `lock_timeout` 5 s : `orgs` est lue à chaque
requête ; un échec propre se rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose les colonnes avant de servir ;
l'ancien code les ignore (il ne prête rien).

Retour arrière : retire les colonnes (les apps qui prêtaient reviennent à auteur ∩
lecteur).

Révision : 20261124_1000_app_act_as
Précédente : 20261123_1433_embed_file
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import PRET

revision = "20261124_1000_app_act_as"
down_revision = "20261123_1433_embed_file"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(PRET)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for col in ("acts_as", "acts_as_manifest", "acts_as_at"):
        op.execute(f"ALTER TABLE apps DROP COLUMN IF EXISTS {col}")
    op.execute("ALTER TABLE orgs DROP COLUMN IF EXISTS apps_act_as")
