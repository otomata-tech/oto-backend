"""agents_partages_a_l_org : chaque agent existant reste modifiable par toute son org.

Avant le partage d'agents (`capabilities/_acces_agent.py`), tout membre de l'org
modifiait n'importe quel agent hébergé. Désormais un agent est à son propriétaire, qui
le partage nommément. Pour ne rien retirer à personne en silence, chaque agent DÉJÀ
posé reçoit un partage `editor` à son org entière (`principal_type='org'`) — l'écran
« Partager » le montre (« Everyone in the org »), son propriétaire le retire s'il veut
le fermer. Un agent créé ENSUITE naît privé.

⚠️⚠️ **À JOUER AVANT de déployer le code qui lit ces partages** — une révision n'est
pas jouée au démarrage (`docs/migrations-versionnees.md`), c'est un geste
d'exploitation (`oto-mcp migrer upgrade head`, par le lanceur). Déployé sans elle,
le nouveau code rend PRIVÉ chaque agent existant : les membres qui les modifient
aujourd'hui les perdent de vue, sans un mot. Jouée avant, elle ne change rien à
l'ancien code (qui ignore ces lignes) — et la base est PARTAGÉE prod/preprod, donc
une seule fois suffit pour les deux.

Un agent créé par l'ancien code ENTRE la révision et le déploiement naît sans ce
partage, donc privé une fois le nouveau code servi — comme tout agent neuf. Alembic
ne rejoue pas une révision appliquée : pour le rattraper, relancer à la main l'INSERT
d'`upgrade()` (idempotent) après le déploiement.

Idempotente : `ON CONFLICT DO NOTHING` — un partage déjà posé (ou retiré puis reposé
autrement) n'est pas réécrit. Volume : une ligne par agent (quelques dizaines).

Retour arrière : retire les partages d'org posés par cette révision (`granted_by`).

Révision : 0032_agents_partages_a_l_org
Précédente : 0031_selection_org_reelle
"""
from __future__ import annotations

from alembic import op

revision = "0032_agents_partages_a_l_org"
down_revision = "0031_selection_org_reelle"
branch_labels = None
depends_on = None

_MARQUE = "migration:0032_agents_partages_a_l_org"


def upgrade() -> None:
    op.execute(f"""
INSERT INTO resource_grants
       (resource_type, resource_id, principal_type, principal_id, permission, role,
        granted_by)
SELECT 'runner_trigger', id::text, 'org', org_id::text, 'write', 'editor', '{_MARQUE}'
  FROM runner_triggers
ON CONFLICT (resource_type, resource_id, principal_type, principal_id) DO NOTHING""")


def downgrade() -> None:
    op.execute(f"""
DELETE FROM resource_grants
 WHERE resource_type = 'runner_trigger' AND principal_type = 'org'
   AND granted_by = '{_MARQUE}'""")
