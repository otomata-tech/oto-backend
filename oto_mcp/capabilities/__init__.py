"""Couche capacité (ADR 0009) : descripteurs co-déclarés + adaptateurs MCP/REST.

Importer les modules de domaine ICI peuple `registry.CAPABILITIES` à l'import
du package — avant que `server.py` / `api_routes.py` ne bouclent dessus.
"""
from . import _mcp_adapter, _rest_adapter, registry
from . import orgs  # noqa: F401 — peuple registry.CAPABILITIES (org.use_org)
from . import orgs_members  # noqa: F401 — org.member.{add,set_role,remove}
from . import orgs_secrets  # noqa: F401 — org.secret.{set,delete}
from . import orgs_update  # noqa: F401 — org.update (rename / re-describe)
from . import orgs_admin  # noqa: F401 — org.admin.create + org.entitlement.{grant,revoke}
from . import orgs_reads  # noqa: F401 — org.list/get/admin.list/get + member/secret/entitlement.list
from . import orgs_field_filters  # noqa: F401 — org.field_filters.{get,set} (ADR 0015)
from . import orgs_email_settings  # noqa: F401 — org.email_settings.{get,set} (envoi per-org)
from . import scheduled_emails  # noqa: F401 — org.scheduled_email.{list,cancel} (envoi différé)
from . import orgs_invites  # noqa: F401 — org.invite.{create,list,revoke,accept} + platform.invite.alpha
from . import access_admin  # noqa: F401 — platform.access.{waitlist,grant,set_quota} (ADR 0013)
# Sous-palier groupe (ADR 0012) — départements/équipes + chef d'équipe.
from . import groups  # noqa: F401 — group.create/list/list_mine/use/clear/get/update/delete
from . import groups_members  # noqa: F401 — group.member.{add,set_role,remove}
from . import groups_secrets  # noqa: F401 — group.secret.{set,delete} + group.preset.set
from . import groups_doctrine  # noqa: F401 — group.instruction.{list,get,set,delete,versions,revert}
# Palier billing — solde de credits par org, historique, packs, recharge Stripe.
from . import billing  # noqa: F401 — billing.{balance,transactions,packs,checkout}
# Signaux d'usage (ADR 0017) — feedback volontaire sur un outil + remontée des manques.
from . import usage  # noqa: F401 — usage.feedback (signal=tool_feedback|gap) + projections
# Doctrine & instructions d'org (ADR 0009) — CRUD doctrine + usage, membre + admin.
from . import orgs_instructions  # noqa: F401 — org.{doctrine.*, instruction.*}
# Bibliothèque publique de doctrines (marketplace) — list/get/publish/fork/unpublish.
from . import doctrine_library  # noqa: F401 — library.{list,get,publish,fork,unpublish}
# Sélection de connecteurs (marketplace, ADR 0019) — me/select/pause/unselect.
from . import connectors_selection  # noqa: F401 — connectors.{me,select,pause,unselect}
# Plafond DUR d'org (ADR 0022) — override d'activation per-org, éditable org_admin.
from . import connectors_activation  # noqa: F401 — connectors.activation.{org_list,set_org,clear_org}

__all__ = ["registry", "_mcp_adapter", "_rest_adapter"]
