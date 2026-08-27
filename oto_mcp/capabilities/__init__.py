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
from . import orgs_invites  # noqa: F401 — org.invite.{create,list,revoke,accept}
# Invitation PLATEFORME (feature cascade, sommet) — onboarding admin, org cible optionnelle.
from . import platform_invites  # noqa: F401 — platform.invite.{create,list,revoke}
from . import users_admin  # noqa: F401 — platform.user.{list,get,set_role}, platform.{key,org}.{grant,revoke}_key, platform.option.set
from . import vault_health  # noqa: F401 — admin.vault_health (scan credentials indéchiffrables, #72)
from . import tenants_admin  # noqa: F401 — admin.tenant{s,_console} (suivi de l'étage tenant, ADR 0052)
from . import unipile_seats  # noqa: F401 — admin.unipile_seats + admin.unipile_seat_release
from . import editor_apps  # noqa: F401 — platform.editor_app.{list,set,delete} (app OAuth d'oto)
# Sous-palier groupe (ADR 0012) — départements/équipes + chef d'équipe.
from . import groups  # noqa: F401 — group.create/list/list_mine/use/clear/get/update/delete
from . import groups_members  # noqa: F401 — group.member.{add,set_role,remove}
from . import groups_invites  # noqa: F401 — group.invite.{create,list,revoke} (invitation d'équipe)
from . import groups_secrets  # noqa: F401 — group.secret.{set,delete}
from . import groups_doctrine  # noqa: F401 — group.instruction.{list,get,set,delete,versions,revert}
# Signaux d'usage (ADR 0017) — feedback volontaire sur un outil + remontée des manques.
from . import usage  # noqa: F401 — usage.feedback (signal=tool_feedback|gap) + projections
# Lentilles monitoring + console d'investigation (après usage : la console réutilise ses handlers).
from . import monitoring  # noqa: F401 — monitoring.{summary,rest,connectors,funnel,calls,call} + oto_admin_monitoring
from . import me_legal  # noqa: F401 — me.legal.{get,accept} (gate d'acceptation légale)
from . import tenant_legal_docs_admin  # noqa: F401 — admin.legal_docs.{list,set,delete} (override par tenant)
# Abonnement par org (ADR 0043) — REST-only : subscribe/confirm/cancel (org_admin) + status.
from . import billing  # noqa: F401 — billing.{plans,status,subscribe,confirm,cancel,payments}
# Doctrine & instructions d'org (ADR 0009) — CRUD doctrine + usage, membre + admin.
from . import orgs_instructions  # noqa: F401 — org.{doctrine.*, instruction.*}
# Bibliothèque publique de doctrines (marketplace) — list/get/publish/fork/unpublish.
from . import doctrine_library  # noqa: F401 — library.{list,get,publish,fork,unpublish}
# Sélection de connecteurs (marketplace, ADR 0019) — me/select/pause/unselect.
from . import connectors_selection  # noqa: F401 — connectors.{me,select,pause,unselect}
from . import connectors_identities  # noqa: F401 — connectors.{identities,set_default_identity} (ADR 0024)
from . import connectors_connect  # noqa: F401 — me.connector_connect (POST /api/me/connectors/{name}/connect, chemin FIXE)
from . import salesforce_connect  # noqa: F401 — me.salesforce_connect (oto_salesforce_connect ; REST = le chemin fixe /connect)
from . import zoho_connect  # noqa: F401 — me.zoho_connect (oto_zoho_connect + GET /api/zoho/oauth/{start,modes})
from . import connectors_verify  # noqa: F401 — connectors.verify (sonde de credential — MCP via oto_instance op=verify)
# Credential PERSONNEL (pose/état/retrait) — REST-only, un secret ne passe pas
# en argument d'outil. Ex-routes écrites à la main d'`api_routes.py` (#121).
from . import me_credentials  # noqa: F401 — me.credential.{get,set,clear}
# Connexion par SESSION NAVIGATEUR (ADR 0026) — l'autre voie de pose d'un credential,
# par un login humain dans un navigateur hébergé. Ex-routes écrites à la main (#121).
from . import browser_sessions  # noqa: F401 — me.browser_session.{start,finalize}
# Plafond DUR d'org (ADR 0022) — override d'activation per-org, éditable org_admin.
from . import connectors_activation  # noqa: F401 — connectors.activation.{org_list,set_org,clear_org}
# Palier PLATEFORME de la même famille (#121) — cran d'activation global + accès
# plateforme (ADR 0010 B4, ADR 0044 §H). Ex-routes écrites à la main : les paliers org
# et équipe étaient déjà des capacités, c'est l'étage qui manquait.
from . import platform_connectors  # noqa: F401 — platform.connector.{activation_*,access_*}
# RBAC connecteur interne à l'org (ADR 0025) — restreindre un connecteur à des départements/membres.
from . import connectors_acl  # noqa: F401 — connectors.acl.{list,grant,revoke}
# Toolbox du MEMBRE (#121) — ex-routes écrites à la main : liste, registre, bascule
# de visibilité, fiche, test. ⚠️ Six d'un bloc : `…/tools/registry` doit précéder
# `…/tools/{name}`, et l'ordre des bindings est l'ordre de déclaration du module.
from . import tools_me  # noqa: F401 — me.tools.{list,registry,disable,enable,detail,call}
# Denylist de TOOLS par org/équipe — remplace l'ancienne baseline allowlist (retirée 3951a57).
from . import tools_visibility  # noqa: F401 — tools.{org,group}_{list,hide,unhide}
# Partage d'instance (ADR 0044) — le propriétaire PRÊTE sa clé à un pair (share_side — MCP via oto_instance op=lend).
from . import connectors_sharing  # noqa: F401 — connectors.lend_instance
# Autorisation de compte connecteur partagé (#55) — le propriétaire accorde l'opération de SON compte.
from . import connectors_account_grants  # noqa: F401 — connectors.account_grants.{list,grant,revoke}
# Forcer un connecteur dans la toolbox d'un membre (ADR 0031) — override positif (allow).
from . import connectors_force  # noqa: F401 — connectors.force.member (MCP via oto_connector op=force)
# Projection lecture du coffre en instances possédées nommées (ADR 0038 B4).
from . import connectors_instances  # noqa: F401 — connectors.instances.list (ADR 0038 B4)
# Console admin consolidée par concept (ADR 0009, fusion *_op) — réutilise les handlers
# de domaine. À importer APRÈS eux (orgs_admin/reads/members, users_admin).
from . import admin_console  # noqa: F401 — admin.{org,org_member,user,key_grant,doctrine,signal}
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
# Observabilité au niveau ORG (après monitoring + audit_log : elle rebranche leurs
# handlers) — les lentilles plateforme bornées à SON org, pour l'org_admin.
from . import org_monitoring  # noqa: F401 — org.monitoring.* (oto_org_monitoring, /api/orgs/{id}/monitoring/*)
# Gouvernance générique des ressources possédées (ADR 0030) — transfert/partage
# d'un objet possédé (datastore pilote), owner ∪ escalade roles.py.
from . import resources  # noqa: F401 — resources.govern (oto_resource)
# Catalogue du registre de capacités (ADR 0030) — alimente l'object-browser admin.
from . import capabilities_catalog  # noqa: F401 — admin.capabilities (GET /api/admin/capabilities)
# Vue de transparence « contexte agent » (otomata-private#49) — ce que Claude reçoit.
from . import agent_context  # noqa: F401 — me.agent_context (GET /api/me/agent-context)
# Guides ON-DEMAND (ADR 0042) — surface REST des how-to (miroir de l'outil MCP oto_guide).
from . import guides  # noqa: F401 — me.guide (MCP oto_guide) + me.guides.{list,get,set,delete} (REST)
# Le COMPTE (#121) — ex-routes écrites à la main d'`api_routes.py` : `GET /api/me`
# est la première requête de tout front qui se branche, et l'OpenAPI n'en décrivait rien.
from . import me_account  # noqa: F401 — me.{get,calls,activity_summary}
# Préférence de langue de l'UI dashboard (niveau USER, REST-only).
from . import user_locale  # noqa: F401 — me.locale.set (PUT /api/me/locale)
# Fiche profil « situation avec oto » (surface REST de oto_profile, édition dashboard).
from . import profile  # noqa: F401 — me.profile.{get,set} (GET/PUT /api/me/profile)
# Blocs d'instructions plateforme A/B (#50) — édition admin plateforme.
from . import platform_instructions  # noqa: F401 — platform.instructions (oto_admin_platform_instructions)
# Projet — couche d'organisation (modèle produit 2026-06-27, owned resource ADR 0030).
from . import projects  # noqa: F401 — me.project (oto_project, POST /api/me/projects)
from . import project_files  # noqa: F401 — me.project_files (oto_project_files, MCP-only, ADR 0032 §3)
from . import kb  # noqa: F401 — me.kb (oto_kb : base de connaissance d'org = zone Documents)
from . import search  # noqa: F401 — me.search (oto_search, lot 3 Ship 1)
from . import inbox  # noqa: F401 — me.inbox (lot 3 Ship 3)
from . import shell  # noqa: F401 — me.shell (/shell v0, surface nœuds précoce)
from . import node_view  # noqa: F401 — me.node (lecture d'un nœud, surface précoce)
from . import node_rows  # noqa: F401 — me.node.rows (lignes d'un tableau, curseur)
from . import docs  # noqa: F401 — me.doc (oto_doc, POST /api/me/docs) — pages d'un projet
from . import uploads  # noqa: F401 — me.upload_url (oto_upload_url) — push out-of-bande de gros contenu (#105)
# Journal de travail du datastore (ADR 0046 b4) — parcours d'une ligne + activité du tableau.
from . import datastore_activity  # noqa: F401 — me.datastore.{row_activity,activity} (REST-only)
# File de travail côté application (signal #362) — réserver depuis un front web.
from . import datastore_claim  # noqa: F401 — me.datastore.{claim_next,claim_row} (REST-only)
from . import datastore_schema  # noqa: F401 — me.datastore.{get,set}_schema (MCP data_get_schema + REST)
# Partage nominatif d'un tableau (#302) — contrat du client HTTP d'oto-core.
from . import datastore_sharing  # noqa: F401 — me.datastore.{list_shares,share,unshare}
# Le tableau lui-même (#302) — lister/créer/renommer/supprimer/ouvrir, ex-routes écrites
# à la main : mêmes chemins, entrée ET sortie déclarées.
from . import datastore_namespaces  # noqa: F401 — me.datastore.{list,create,delete,rename}_namespace + url
# Les lignes (#302) — page/fiche/écriture/suppression/file/agrégat, ex-routes écrites
# à la main. Deux corps LIBRES (les colonnes du tableau), déclarés comme tels.
from . import datastore_rows  # noqa: F401 — me.datastore.{list_rows,append_row,get_row,update_row,delete_row,release_claim,queue,aggregate}
# Purge d'une colonne morte (#296) — après un renommage, l'ancienne clé piège les agents.
from . import datastore_columns  # noqa: F401 — me.datastore.drop_column (MCP data_drop_column)
from . import automation  # noqa: F401 — me.automation.fire (MCP routine_fire + REST)
from . import run_thread  # noqa: F401 — runs.thread append/read (MCP oto_run_thread + REST) — chantier runner R1
from . import runner_jobs  # noqa: F401 — runner.jobs (REST-only, worker) — chantier runner R2
from . import runner_triggers  # noqa: F401 — runner.triggers (MCP oto_trigger + REST) — chantier runner R3

__all__ = ["registry", "_mcp_adapter", "_rest_adapter"]
