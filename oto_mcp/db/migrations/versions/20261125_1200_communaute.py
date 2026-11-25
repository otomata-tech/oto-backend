"""La communauté : retraits du personnel et installations des entrées publiées.

Deux tables NEUVES, nées entières (`db/schema/procedures.py::COMMUNAUTE`) — posées aussi
par le démarrage (`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL :
`community_moderation` (une ligne par entrée retirée, ou restaurée après retrait) et
`community_installs` (une ligne par installation réussie). FK vers `orgs` seulement
(`community_installs.org_id`, ON DELETE CASCADE) ; aucune FK vers l'entrée publiée.

**Verrous.** Tables neuves et vides ; la FK prend un `ShareRowExclusiveLock` bref sur
`orgs`. `lock_timeout` 5 s : un échec net, qu'on rejoue, plutôt qu'une attente.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes tables ; l'ancien code
les ignore (la bibliothèque se lit comme avant, sans retrait ni compteur).

Retour arrière : retire les deux tables (les retraits et les compteurs se perdent ; rien
d'autre ne les lit).

Révision : 20261125_1200_communaute
Précédente : 20261124_1200_empreintes_2fa
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.procedures import COMMUNAUTE

revision = "20261125_1200_communaute"
down_revision = "20261124_1200_empreintes_2fa"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(COMMUNAUTE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS community_installs")
    op.execute("DROP TABLE IF EXISTS community_moderation")
