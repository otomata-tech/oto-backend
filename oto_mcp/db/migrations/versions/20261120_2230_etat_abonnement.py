"""L'état d'abonnement d'une org, poussé par le commerce (`subscription_states`).

Une table NEUVE, née vide, posée aussi par le démarrage (même texte,
`db/schema/orgs.py::SUBSCRIPTION_STATES`). FK vers `orgs`, ON DELETE CASCADE : l'état
part avec l'org. Le commerce la remplit (`service.org.subscription_state`) ; la fiche
d'admin d'org la lit et refuse (`subscription_state_missing`) tant que rien n'a été
poussé pour l'org.

**Verrous.** La FK prend un `ShareRowExclusiveLock` bref sur `orgs`. `lock_timeout` 5 s :
`orgs` est lue à chaque requête ; un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose la table avant de servir ;
l'ancien code l'ignore.

Retour arrière : retire la table (les états poussés se perdent ; le commerce les repousse
par son remplissage).

Révision : 20261120_2230_etat_abonnement
Précédente : 20261120_2220_archive_passages
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.orgs import SUBSCRIPTION_STATES

revision = "20261120_2230_etat_abonnement"
down_revision = "20261120_2220_archive_passages"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(SUBSCRIPTION_STATES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS subscription_states")
