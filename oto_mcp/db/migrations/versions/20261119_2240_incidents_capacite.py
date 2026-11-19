"""Les incidents de capacité d'une org : des agents qu'aucun forfait ne peut faire tourner.

Une table NEUVE, née entière : `org_incidents_capacite`
(`db/schema/runs.py::ORG_INCIDENTS_CAPACITE`), et son index unique PARTIEL (un seul
incident vivant par org et famille) — posés aussi par le démarrage (`CREATE … IF NOT
EXISTS` de l'assemblage), même DDL. Aucune FK. Deux colonnes de comptes (`titulaires`,
qui tient le forfait qui manque ; `titulaires_prevenus`, ceux déjà prévenus) : un état
d'exploitation, que la boucle reconstruit à chaque passe ; une fusion de comptes au pire
réécrit une fois à la même personne.

**Verrous.** Aucun sur une table existante : la table neuve est vide. `lock_timeout` 5 s,
comme les voisines.

**Ordre avec le code : indifférent.** Le démarrage pose la même table ; l'ancien code
l'ignore.

Retour arrière : retire la table (les incidents passés et ce qui en est parti se perdent ;
un incident encore vivant se rouvrira, et s'annoncera de nouveau, au premier passage).

Révision : 20261119_2240_incidents_capacite
Précédente : 20261119_1300_app_use_role
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.runs import ORG_INCIDENTS_CAPACITE

revision = "20261119_2240_incidents_capacite"
down_revision = "20261119_1300_app_use_role"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(ORG_INCIDENTS_CAPACITE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS org_incidents_capacite")
