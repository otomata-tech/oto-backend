"""Les agents nés privés depuis la révision 0035 deviennent ceux de leur org.

Un agent naît désormais partagé à toute son org, en `editor` (`_acces_agent.
ouvrir_a_l_org`, appelé par `runner.triggers create` sauf `private=true`). Entre la
révision 0035 (qui a posé ce partage sur chaque agent alors existant) et ce lot, un agent
naissait PRIVÉ : l'org perdait de vue ses propres automatisations. Cette révision leur pose
le même partage, avec la même forme que 0035.

**Qui est rattrapé** : un agent sans partage vivant ou échu à son org, dont l'id est
PLUS GRAND que le plus grand agent marqué par 0035 — donc né après elle. Un agent plus
ancien sans partage d'org l'a perdu par un geste (son propriétaire ou un admin a fermé
« Everyone ») : on ne rouvre pas en silence ce que quelqu'un a fermé. Les agents nés
après 0035 n'ont jamais eu de partage d'org à fermer : ils n'avaient pas ce choix.
Ni un agent ARMÉ d'`oto_admin_*` : un partage en écriture le désarmerait en silence
(`outils_plateforme_du_travail`) — il reste privé, comme son porteur l'a voulu.

⚠️ À jouer AVANT ou APRÈS le code, indifféremment : l'ancien code lit ces partages
(`_acces_agent.niveaux`) depuis 0035, le nouveau aussi. La base est PARTAGÉE prod/preprod :
une seule exécution suffit pour les deux. Un agent créé par l'ancien code entre la
révision et le déploiement naît privé ; relancer l'INSERT d'`upgrade()` à la main
(idempotent) une fois le code servi le rattrape.

Idempotente : `ON CONFLICT DO NOTHING`. Volume : une ligne par agent rattrapé.

Retour arrière : retire les partages d'org posés par cette révision (`granted_by`).

Révision : 20261023_1000_agents_org_default
Précédente : 20261022_2112_forfait_fenetre
"""
from __future__ import annotations

from alembic import op

revision = "20261023_1000_agents_org_default"
down_revision = "20261022_2112_forfait_fenetre"
branch_labels = None
depends_on = None

_MARQUE = "migration:20261023_agents_org_default"
_MARQUE_0035 = "migration:0035_agents_partages_a_l_org"


def upgrade() -> None:
    op.execute(f"""
INSERT INTO resource_grants
       (resource_type, resource_id, principal_type, principal_id, permission, role,
        granted_by)
SELECT 'runner_trigger', t.id::text, 'org', t.org_id::text, 'write', 'editor', '{_MARQUE}'
  FROM runner_triggers t
 WHERE t.id > COALESCE((SELECT max(g.resource_id::bigint) FROM resource_grants g
                         WHERE g.resource_type = 'runner_trigger'
                           AND g.granted_by = '{_MARQUE_0035}'), 0)
   AND t.outils_plateforme = '[]'::jsonb
   AND NOT EXISTS (SELECT 1 FROM resource_grants g
                    WHERE g.resource_type = 'runner_trigger'
                      AND g.resource_id = t.id::text
                      AND g.principal_type = 'org'
                      AND g.principal_id = t.org_id::text)
ON CONFLICT (resource_type, resource_id, principal_type, principal_id) DO NOTHING""")


def downgrade() -> None:
    op.execute(f"""
DELETE FROM resource_grants
 WHERE resource_type = 'runner_trigger' AND principal_type = 'org'
   AND granted_by = '{_MARQUE}'""")
