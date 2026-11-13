"""Partages : le rôle `contributor` (« Use » d'une app) entre dans `resource_grants`.

Le 13/11/2026, une app se partage en quatre rôles : View (`viewer`), Use
(`contributor`), Edit (`editor`), Manage (`manager`). `contributor` voit l'app et clique
ses boutons — qui agissent avec les droits de l'APP (`capabilities/apps_appel.py`) — sans
la modifier. Sa projection CONTENU est `read`.

Le CHECK `resource_grants_role_check` s'ÉLARGIT : retiré puis reposé sous le même nom,
`NOT VALID` puis validé hors transaction (toutes les lignes existantes y répondent déjà ;
la validation ne prend qu'un `ShareUpdateExclusiveLock`, qui laisse lire et écrire).
Une base neuve le reçoit tel quel du `CREATE TABLE` (`db/schema/grants.py`).

`app_consents.file_ids` (BIGINT[], nullable, sans défaut) : les fichiers qu'un clic
remet au run qu'il lance — le fragment `db/schema/apps.py::CONSENTEMENT_FICHIERS`,
joué tel quel (`ADD COLUMN IF NOT EXISTS`), comme au démarrage.

`contraintes_create_table.RESOURCE_GRANTS_ROLE` suit la forme du `CREATE TABLE` (une
base neuve et une base remise à niveau rendent le même schéma) : 0057 ne se rejoue pas
sur une base versionnée, qui reçoit la forme élargie d'ICI.

**Verrous.** `AccessExclusiveLock` bref sur `resource_grants` (catalogue seul) pour le
retrait et la pose `NOT VALID`. `lock_timeout` 5 s.

**Ordre avec le code : migration d'abord.** L'ancien code tourne sur le CHECK élargi
sans rien voir (il n'écrit jamais `contributor`). Le nouveau code sur l'ancien CHECK
refuserait le partage Use d'une app (violation de contrainte) — rien d'autre.

Retour arrière : les partages `contributor` redeviennent `viewer` (ils voient l'app,
sans ses boutons), puis le CHECK retrouve sa forme d'avant ; `file_ids` part.

⚠️ **Tant que la prod tourne un tag d'avant**, son démarrage ramène chaque partage
`contributor` à `viewer` (son rattrapage de rôle ne connaît pas `contributor`, base
partagée) : ne poser aucun partage Use avant que la prod tourne le tag qui le lit.

Révision : 20261113_1900_app_use_role
Précédente : 20261113_1800_usage_org
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.apps import CONSENTEMENT_FICHIERS

revision = "20261113_1900_app_use_role"
down_revision = "20261113_1800_usage_org"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
_NOM = "resource_grants_role_check"


def _poser(roles: tuple[str, ...]) -> None:
    liste = ", ".join(f"'{r}'" for r in roles)
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE resource_grants DROP CONSTRAINT IF EXISTS {_NOM}")
    op.execute(f"ALTER TABLE resource_grants ADD CONSTRAINT {_NOM} "
               f"CHECK (role IN ({liste})) NOT VALID")
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE resource_grants VALIDATE CONSTRAINT {_NOM}")


def upgrade() -> None:
    _poser(("viewer", "contributor", "editor", "manager"))
    op.execute(_ATTENTE_MAX)
    op.execute(CONSENTEMENT_FICHIERS)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE app_consents DROP COLUMN IF EXISTS file_ids")
    op.execute("UPDATE resource_grants SET role = 'viewer', permission = 'read' "
               "WHERE role = 'contributor'")
    _poser(("viewer", "editor", "manager"))
