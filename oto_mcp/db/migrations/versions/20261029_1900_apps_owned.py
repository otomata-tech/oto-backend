"""Apps : un propriétaire et des partages à elles ; un projet ne fait plus que les
ranger (`apps.owner_type/owner_id/context_org_id`, `app_projects`).

DDL : le fragment `db/schema/apps.py::PROPRIETE`, exécuté tel quel (colonnes ajoutées,
`project_id` rendu facultatif, table `app_projects`).

DONNÉES, une fois, pour les apps déjà là — personne ne perd l'accès qu'il avait :
1. propriétaire = celui du projet de l'app ; org de création = celle du projet ;
2. l'app reste RANGÉE dans son projet (`app_projects`) ;
3. les partages du PROJET sont recopiés sur l'app (`resource_grants`, type `app`),
   rôle, échéance et auteur compris ;
4. `apps.project_id` est vidé : il ne sert plus à rien, et le garder rempli ferait
   croire qu'il décide encore.

**Verrous.** `ALTER TABLE apps` prend un `AccessExclusiveLock` bref (quelques lignes) ;
`lock_timeout` 5 s.

**Ordre avec le code : la migration AVANT le code.** Le nouveau code juge l'accès sur le
propriétaire de l'app : une app sans propriétaire n'est lisible par personne. L'ancien
code, lui, lit `project_id` : après cette migration il ne trouverait plus de projet.
⚠️ **Entre le tag et `migrer upgrade head`, les apps sont INVISIBLES** (fail-closed :
sans propriétaire, `can_access` refuse ; aucune fuite, mais aucune app ne s'ouvre ni ne
se liste). Le démarrage ne joue PAS la partie DONNÉES (seul le DDL additif du fragment
est rejoué au boot) : jouer `migrer upgrade head` JUSTE APRÈS le tag.

Index : `apps (owner_id)` et `app_projects (filed_by)` — la fusion de comptes repointe
ces deux colonnes-compte d'un bloc (`db/users._SUB_COLUMNS`), sans index elle
balaierait la table.

Retour arrière : remet `project_id` depuis `app_projects`, retire les partages d'app,
les colonnes et la table.

Révision : 20261029_1900_apps_owned
Précédente : 20261029_1800_app_consents
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import PROPRIETE

revision = "20261029_1900_apps_owned"
down_revision = "20261029_1800_app_consents"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(PROPRIETE)
    op.execute(
        "UPDATE apps a SET owner_type = p.owner_type, owner_id = p.owner_id, "
        "context_org_id = p.context_org_id "
        "FROM projects p WHERE p.id = a.project_id AND a.owner_type IS NULL")
    op.execute(
        "INSERT INTO app_projects (app_id, project_id, filed_by) "
        "SELECT id, project_id, created_by FROM apps WHERE project_id IS NOT NULL "
        "ON CONFLICT DO NOTHING")
    op.execute(
        "INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
        "principal_id, permission, role, granted_by, granted_at, expires_at) "
        "SELECT 'app', a.id::text, g.principal_type, g.principal_id, g.permission, "
        "g.role, g.granted_by, g.granted_at, g.expires_at "
        "FROM apps a JOIN resource_grants g "
        "ON g.resource_type = 'project' AND g.resource_id = a.project_id::text "
        "ON CONFLICT DO NOTHING")
    op.execute("UPDATE apps SET project_id = NULL WHERE project_id IS NOT NULL")


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(
        "UPDATE apps a SET project_id = f.project_id FROM ("
        "SELECT DISTINCT ON (app_id) app_id, project_id FROM app_projects "
        "ORDER BY app_id, filed_at) f WHERE f.app_id = a.id AND a.project_id IS NULL")
    op.execute("DELETE FROM resource_grants WHERE resource_type = 'app'")
    op.execute("DROP TABLE IF EXISTS app_projects")
    op.execute("DROP INDEX IF EXISTS apps_owner_idx")
    op.execute("DROP INDEX IF EXISTS apps_context_org_idx")
    op.execute("ALTER TABLE apps DROP COLUMN IF EXISTS context_org_id")
    op.execute("ALTER TABLE apps DROP COLUMN IF EXISTS owner_id")
    op.execute("ALTER TABLE apps DROP COLUMN IF EXISTS owner_type")
