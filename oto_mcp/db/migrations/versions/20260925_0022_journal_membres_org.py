"""Journal des entrées et sorties de membres d'une org (otomata-tech/oto#145).

Un seul geste, additif : la table NEUVE `org_member_events` et son index, fragment
`db/schema/orgs.py::MEMBER_EVENTS` exécuté tel quel. Le démarrage la crée aussi
(`CREATE TABLE IF NOT EXISTS`, sauté par le garde des DDL une fois posée) : ils sont
idempotents l'un envers l'autre, et une base neuve la reçoit du démarrage (§5.2 de
`docs/migrations-versionnees.md`). Sa clé étrangère vers `orgs` prend un verrou sur
`orgs` à la création, borné par `lock_timeout`.

**Aucun `ALTER` sur une table existante**, aucune donnée reprise : le journal commence
au déploiement, les gestes antérieurs n'ont laissé aucune trace à reconstituer.

⚠️ Prod et préprod partagent la MÊME base. L'ancien code ne lit ni n'écrit la table :
jouer cette révision avant ou après le déploiement ne change rien pour lui. Le code du
lot, lui, l'ÉCRIT dans la transaction de chaque ajout, retrait ou changement de rôle
d'un membre — sans elle, ces gestes échoueraient en `UndefinedTable`. À jouer AVANT la
fusion, pour fermer la fenêtre où un démarrage raté sur `lock_timeout` la laisserait
absente. Le retour arrière retire la table et l'historique qu'elle porte ; le code qui
l'écrit doit être retiré avant lui.

Révision : 0022_journal_membres_org
Précédente : 0021_limites_du_run
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.orgs import MEMBER_EVENTS

revision = "0022_journal_membres_org"
down_revision = "0021_limites_du_run"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(MEMBER_EVENTS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS org_member_events")
