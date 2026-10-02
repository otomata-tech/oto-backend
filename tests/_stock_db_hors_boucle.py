"""Stock GELÉ des sites `async def` qui touchent la base DANS la boucle (cliquet).

`site -> par quels appels il atteint la base` (le nom de l'appel, pas la ligne : elle
bougerait au moindre commit). Produit par `tests/_appels_db_hors_boucle.py` ; posé le
21/09/2026 avec la garde d'exécution (`oto_mcp/db/_hors_boucle.py`), rétréci depuis à chaque
lot de décharge.

**Cette liste ne fait que RÉTRÉCIR.** `tests/test_db_hors_boucle.py` échoue

- si un `async def` NOUVEAU atteint la base dans la boucle (il n'est pas ici) ;
- si un site listé n'est plus fautif (il a été corrigé : retire sa ligne, dans le MÊME
  commit — sinon la liste tolérerait sa régression).

Ajouter une ligne ici pour faire passer un test rouge est le geste interdit : un site
en défaut est un gel de production en attente (`docs/event-loop-perf.md`). On décharge
(`await run_in_threadpool(...)`), on ne tolère pas.

Les sites marqués `[dormant]` ne touchent la base QUE par l'identité sous drain d'alias
(`auth.hooks.current_user_sub_from_token`, pure hors du drain) : réels le jour où le drain
s'arme, sans effet tant qu'il est désarmé. À décharger quand même — c'est un interrupteur.
"""
from __future__ import annotations

STOCK: dict[str, str] = {
    "oto_mcp.api.billing::make_routes.<locals>.invoice_pdf":
        "roles.is_org_member",
    "oto_mcp.api.datastore_export::export_csv":
        "ns_not_found",
    "oto_mcp.api.media::avatar_save":
        "db.get_user, db.set_avatar_url",
    "oto_mcp.api.media::org_logo_save":
        "_org_logo_gate, org_store.get_org, org_store.set_org_logo",
    "oto_mcp.api.projects::me_project_export":
        "_project_org_context_error, db.get_project_by_id, db.list_docs_for_project, ownership.can_access",
    "oto_mcp.api.projects::project_files_upload":
        "_project_org_context_error, db.add_project_file, db.get_project_by_id, db.log_project_activity, ownership.can_access",
    "oto_mcp.call_axes::_pin_group":
        "[dormant] require_axis_sub",
    "oto_mcp.call_axes::_pin_instance":
        "[dormant] require_axis_sub",
    "oto_mcp.call_axes::_pin_project":
        "[dormant] require_axis_sub",
    "oto_mcp.call_axes::resolve_org_guarded":
        "[dormant] require_axis_sub",
    "oto_mcp.capabilities.agent_toolbox::_toolbox":
        "connector_activation.exposed_connectors, connector_selection.list_selection_detail",
    "oto_mcp.capabilities.browser_sessions::_finalize":
        "access.current_group, access.current_org, roles.can_admin_group, roles.is_org_admin",
    "oto_mcp.capabilities.connectors.console::_instance":
        "connectors_instances._list_instances, connectors_sharing._lend_instance",
    "oto_mcp.capabilities.connectors.force::_force_connector":
        "_resolve_member, appliquer_servi",
    "oto_mcp.capabilities.connectors.identities::_list":
        "_require_scope, _why_empty",
    "oto_mcp.capabilities.connectors.identities::_set_default":
        "_require_scope",
    "oto_mcp.capabilities.connectors.verify::_verify":
        "_fields_config_scope, connector_health.record_health",
    "oto_mcp.capabilities.instance_health::_instance_health":
        "credentials_store.get_credential",
    "oto_mcp.capabilities.unipile_me::_status":
        "unipile.status_for",
    "oto_mcp.connectors.identities::_unipile_list":
        "_unipile_chosen, _unipile_client, access.current_org, db.list_account_grants_to, db.list_unipile_accounts",
    "oto_mcp.connectors.identities::_unipile_live_status_map":
        "access.resolve_credential",
    "oto_mcp.connectors.identities::_unipile_select":
        "_unipile_client, access.current_org, db.clear_operated_account, db.list_account_grants_to, db.list_unipile_accounts, db.set_operated_account, db.set_unipile_account",
    "oto_mcp.middleware.account_suspended::AccountSuspendedMiddleware.on_request":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.middleware.alias::ToolAliasMiddleware.on_call_tool":
        "[dormant] self._prefix",
    "oto_mcp.middleware.alias::ToolAliasMiddleware.on_initialize":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.middleware.alias::ToolAliasMiddleware.on_list_tools":
        "[dormant] self._prefix",
    "oto_mcp.middleware.call_context::CallContextMiddleware.on_call_tool":
        "call_axes.axes_for_call",
    "oto_mcp.middleware.call_context::CallContextMiddleware.on_list_tools":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.middleware.disabled_tools::UserDisabledToolsMiddleware.on_initialize":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.middleware.dynamic_instructions::DynamicInstructionsMiddleware.on_initialize":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.middleware.dynamic_instructions::DynamicInstructionsMiddleware.on_list_tools":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.middleware.field_redaction::FieldRedactionMiddleware.on_call_tool":
        "_observe_schema, redaction.redact_payload",
    "oto_mcp.run_org::pin_for_call":
        "[dormant] call_axes.current_user_sub_from_token",
    "oto_mcp.sentry_setup::SentryToolErrorMiddleware.on_call_tool":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.server::_IatGatedVerifier.verify_token":
        "self._audience_ok",
    "oto_mcp.server::_build_mcp.<locals>._calllog_sink":
        "access.current_org, current_user_sub_from_token",
    "oto_mcp.tools.brevoauto::_api":
        "_context_id",
    "oto_mcp.tools.brevoauto::register.<locals>.brevoauto_connect_status":
        "[dormant] _sub",
    "oto_mcp.tools.browser::register.<locals>.browser_connect_status":
        "[dormant] _sub",
    "oto_mcp.tools.browser::register.<locals>.browser_eval":
        "_context_id",
    "oto_mcp.tools.browser::register.<locals>.browser_fetch":
        "_context_id",
    "oto_mcp.tools.crunchbase::register.<locals>.crunchbase_connect_status":
        "[dormant] _sub",
    "oto_mcp.tools.drive::register.<locals>.drive_file":
        "[dormant] access.current_user_sub_or_raise",
    "oto_mcp.tools.gmail::register.<locals>.gmail_message":
        "[dormant] access.current_user_sub_or_raise",
    "oto_mcp.tools.guide_run::_note_procedure_version":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.tools.guide_run::_persist_close":
        "[dormant] current_user_sub_from_token",
    "oto_mcp.tools.guide_run::_persist_open":
        "access.current_org, current_user_sub_from_token",
    "oto_mcp.tools.instagram_meta_session::_client":
        "[dormant] access.current_user_sub_or_raise",
    "oto_mcp.tools.meta::_trace_target_call":
        "access.current_org",
    "oto_mcp.tools.meta::register.<locals>.oto_call":
        "_tool_prefix, current_user_sub_from_token",
    "oto_mcp.tools.meta::register.<locals>.oto_tool_schema":
        "[dormant] _require_sub, _tool_prefix",
    "oto_mcp.tools.pennylaneged::_call_raw":
        "_context_id",
    "oto_mcp.tools.pennylaneged_session::register.<locals>.pennylaneged_connect_status":
        "[dormant] _sub",
    "oto_mcp.tools.unipile::register.<locals>.unipile_connect_start":
        "[dormant] access.current_user_sub_or_raise",
    "oto_mcp.transport_refusals::TransportRefusalCounter.__call__.<locals>._send":
        "enregistrer",
    "oto_mcp.unipile_connect::hosted_auth_url":
        "access.current_org, access.has_option, db.count_unipile_accounts_for_org, db.create_unipile_pending, db.get_org_unipile_limit, db.get_unipile_account, db.get_unipile_account_id, db.list_unipile_accounts, db.seat_binding_elsewhere, db.set_unipile_account",
}
