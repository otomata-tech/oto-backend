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
from . import orgs_mfa  # noqa: F401 — org.mfa.{get,set} (MFA obligatoire par org, miroir Logto)
from . import scheduled_emails  # noqa: F401 — org.scheduled_email.{list,cancel} (envoi différé)
from . import orgs_invites  # noqa: F401 — org.invite.{create,list,revoke,accept} + platform.invite.alpha
from . import access_admin  # noqa: F401 — platform.access.{waitlist,grant,set_quota} (ADR 0013)
from . import users_admin  # noqa: F401 — platform.user.{list,get,set_role}, platform.{key,org}.{grant,revoke}_key, platform.option.set
# Sous-palier groupe (ADR 0012) — départements/équipes + chef d'équipe.
from . import groups  # noqa: F401 — group.create/list/list_mine/use/clear/get/update/delete
from . import groups_members  # noqa: F401 — group.member.{add,set_role,remove}
from . import groups_secrets  # noqa: F401 — group.secret.{set,delete}
from . import groups_doctrine  # noqa: F401 — group.instruction.{list,get,set,delete,versions,revert}
# Signaux d'usage (ADR 0017) — feedback volontaire sur un outil + remontée des manques.
from . import usage  # noqa: F401 — usage.feedback (signal=tool_feedback|gap) + projections
# Abonnement par org (ADR 0043) — REST-only : subscribe/confirm/cancel (org_admin) + status.
from . import billing  # noqa: F401 — billing.{plans,status,subscribe,confirm,cancel,payments}
# Doctrine & instructions d'org (ADR 0009) — CRUD doctrine + usage, membre + admin.
from . import orgs_instructions  # noqa: F401 — org.{doctrine.*, instruction.*}
# Bibliothèque publique de doctrines (marketplace) — list/get/publish/fork/unpublish.
from . import doctrine_library  # noqa: F401 — library.{list,get,publish,fork,unpublish}
# Sélection de connecteurs (marketplace, ADR 0019) — me/select/pause/unselect.
from . import connectors_selection  # noqa: F401 — connectors.{me,select,pause,unselect}
from . import connectors_identities  # noqa: F401 — connectors.{identities,set_default_identity} (ADR 0024)
from . import connectors_verify  # noqa: F401 — connectors.verify (sonde de credential — MCP via oto_instance op=verify)
# Plafond DUR d'org (ADR 0022) — override d'activation per-org, éditable org_admin.
from . import connectors_activation  # noqa: F401 — connectors.activation.{org_list,set_org,clear_org}
# RBAC connecteur interne à l'org (ADR 0025) — restreindre un connecteur à des départements/membres.
from . import connectors_acl  # noqa: F401 — connectors.acl.{list,grant,revoke}
# Partage d'instance (ADR 0044) — le propriétaire PRÊTE sa clé à un pair (share_side — MCP via oto_instance op=lend).
from . import connectors_sharing  # noqa: F401 — connectors.lend_instance
# Autorisation de compte connecteur partagé (#55) — le propriétaire accorde l'opération de SON compte.
from . import connectors_account_grants  # noqa: F401 — connectors.account_grants.{list,grant,revoke}
# Forcer un connecteur dans la toolbox d'un membre (ADR 0031) — override positif (allow).
from . import connectors_force  # noqa: F401 — connectors.force.member (MCP via oto_connector op=force)
# Projection lecture du coffre en instances possédées nommées (ADR 0038 B4).
from . import connectors_instances  # noqa: F401 — connectors.instances.list (ADR 0038 B4)
# Console admin consolidée par concept (ADR 0009, fusion *_op) — réutilise les handlers
# de domaine. À importer APRÈS eux (orgs_admin/reads/members, users_admin, access_admin).
from . import admin_console  # noqa: F401 — admin.{org,org_member,user,access,key_grant}
# Console connecteurs consolidée (ADR 0047 B1, fusion *_op) — porte les 6 tools MCP de la
# famille (activation/access/connector/instance/identity/account_access) ; les modules
# ci-dessus gardent leurs faces REST. À importer APRÈS eux.
from . import connectors_console  # noqa: F401 — connectors.console.{activation,access,connector,instance,identity,account_access}
# Console procédures (ADR 0047 B2) — oto_procedure (doctrine membre + bibliothèque publique).
from . import procedure_console  # noqa: F401 — org.procedure.console
# Console org/équipe (ADR 0047 B3) — oto_org, oto_org_settings, oto_group, oto_scheduled_emails.
from . import org_console  # noqa: F401 — org.console + org.settings.console + group.console + org.scheduled_emails.console
# Export du journal d'audit org-scopé (oto-backend#67) — REST-only, org_admin.
from . import audit_log  # noqa: F401 — org.audit_log.export (GET /api/orgs/{id}/audit-log/export)
# Gouvernance générique des ressources possédées (ADR 0030) — transfert/partage
# d'un objet possédé (datastore pilote), owner ∪ escalade roles.py.
from . import resources  # noqa: F401 — resources.govern (oto_resource)
# Catalogue du registre de capacités (ADR 0030) — alimente l'object-browser admin.
from . import capabilities_catalog  # noqa: F401 — admin.capabilities (GET /api/admin/capabilities)
# Vue de transparence « contexte agent » (otomata-private#49) — ce que Claude reçoit.
from . import agent_context  # noqa: F401 — me.agent_context (GET /api/me/agent-context)
# Agent README personnel (niveau USER du concept agent_readme, cumulable).
from . import agent_readme  # noqa: F401 — me.agent_readme.{get,set} (GET/PUT /api/me/agent-readme)
# Guides ON-DEMAND (ADR 0042) — surface REST des how-to (miroir de l'outil MCP oto_guide).
from . import guides  # noqa: F401 — me.guides.{list,get,set,delete} (/api/me/guides…)
# Préférence de langue de l'UI dashboard (niveau USER, REST-only).
from . import user_locale  # noqa: F401 — me.locale.set (PUT /api/me/locale)
# Fiche profil « situation avec oto » (surface REST de oto_profile, édition dashboard).
from . import profile  # noqa: F401 — me.profile.{get,set} (GET/PUT /api/me/profile)
# Blocs d'instructions plateforme A/B (#50) — édition admin plateforme.
from . import platform_instructions  # noqa: F401 — platform.instructions (oto_admin_platform_instructions)
# Projet — couche d'organisation (modèle produit 2026-06-27, owned resource ADR 0030).
from . import projects  # noqa: F401 — me.project (oto_project, POST /api/me/projects)
from . import project_files  # noqa: F401 — me.project_files (oto_project_files, MCP-only, ADR 0032 §3)
from . import kb  # noqa: F401 — me.kb (oto_kb : base de connaissance d'org = zone Documents, remplace Memento)
from . import docs  # noqa: F401 — me.doc (oto_doc, POST /api/me/docs) — pages d'un projet
from . import uploads  # noqa: F401 — me.upload_url (oto_upload_url) — push out-of-bande de gros contenu (#105)

__all__ = ["registry", "_mcp_adapter", "_rest_adapter"]
