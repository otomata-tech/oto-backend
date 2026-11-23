"""Les écrivains de service d'un tableau : quel service machine peut écrire ses lignes.

Une table NEUVE, née entière : `datastore_service_writers`
(`db/schema/datastore.py::ECRIVAINS_DE_SERVICE`) — posée aussi par le démarrage (`CREATE
TABLE IF NOT EXISTS` de l'assemblage), même DDL. FK vers `user_datastores` (ON DELETE
CASCADE). Aucune donnée reprise : sans ligne, aucun service n'écrit aucun tableau, rien
ne change.

**Verrous.** Table neuve et vide ; sa FK prend un `ShareRowExclusiveLock` bref sur
`user_datastores`. `lock_timeout` 5 s : un échec net, qu'on rejoue, plutôt qu'une attente.

**Ordre avec le code : indifférent.** Le démarrage pose la même table ; l'ancien code
l'ignore.

Retour arrière : retire la table. Les tableaux ouverts à un service ne le sont plus ; le
service reçoit 404 sur chaque écriture.

Révision : 20261122_2300_ecrivains_service
Précédente : 20261120_2230_etat_abonnement
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.datastore import ECRIVAINS_DE_SERVICE

revision = "20261122_2300_ecrivains_service"
down_revision = "20261120_2230_etat_abonnement"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(ECRIVAINS_DE_SERVICE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS datastore_service_writers")
