"""Couche capacité (ADR 0009) : descripteurs co-déclarés + adaptateurs MCP/REST.

Importer les modules de domaine ICI peuple `registry.CAPABILITIES` à l'import
du package — avant que `server.py` / `api_routes.py` ne bouclent dessus.
"""
from . import _mcp_adapter, _rest_adapter, registry
from . import orgs  # noqa: F401 — peuple registry.CAPABILITIES (org.use_org)
from . import orgs_members  # noqa: F401 — org.member.{add,set_role,remove}
from . import orgs_secrets  # noqa: F401 — org.secret.{set,delete}
from . import orgs_admin  # noqa: F401 — org.admin.create + org.entitlement.{grant,revoke}
from . import orgs_reads  # noqa: F401 — org.list/get/admin.list/get + member/secret/entitlement.list
from . import orgs_invites  # noqa: F401 — org.invite.{create,list,revoke,accept} + org.create
# Sous-palier groupe (ADR 0012) — départements/équipes + chef d'équipe.
from . import groups  # noqa: F401 — group.create/list/list_mine/use/clear/get/update/delete
from . import groups_members  # noqa: F401 — group.member.{add,set_role,remove}
from . import groups_secrets  # noqa: F401 — group.secret.{set,delete} + group.preset.set
from . import groups_doctrine  # noqa: F401 — group.instruction.{list,get,set,delete,versions,revert}

__all__ = ["registry", "_mcp_adapter", "_rest_adapter"]
