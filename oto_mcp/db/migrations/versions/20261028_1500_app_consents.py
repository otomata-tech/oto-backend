"""Apps : le consentement d'un lecteur à un geste d'app (`app_consents`, #1386).

Une table NEUVE, née entière — le DDL est le fragment
`db/schema/apps.py::CONSENTEMENTS`, exécuté tel quel : un jeton à usage unique par
geste (écrire une valeur, ajouter une ligne, lancer un agent), stocké en empreinte, et
qui reste une fois brûlé comme journal des gestes d'app.

**Verrous.** `AccessExclusiveLock` sur la table neuve (vide, instantané) ; la clé
étrangère vers `apps` prend un `ShareRowExclusiveLock` bref. `lock_timeout` 5 s.

**Ordre avec le code : indifférent.** Le démarrage pose la même table (`CREATE TABLE
IF NOT EXISTS`, même fragment) ; l'ancien code l'ignore.

Retour arrière : retire la table (les consentements en cours se perdent ; le front
redemande).

Révision : 20261028_1500_app_consents
Précédente : 20261027_1800_row_comments
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import CONSENTEMENTS

revision = "20261028_1500_app_consents"
down_revision = "20261027_1800_row_comments"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(CONSENTEMENTS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS app_consents")
