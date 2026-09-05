"""La surface plate `db.<fn>` ne perd rien au découpage (#325).

Le module datastore se découpe selon ses coutures — le fichier étant l'unité
d'occupation d'une session sur un tree partagé, quatre chantiers ont dû entrer dans les
trois mêmes fichiers en une semaine, d'où des gels en série et un incident de tree.

Le découpage est un DÉPLACEMENT PUR : aucun appelant ne doit voir la différence. Ce
test fige les noms publiquement atteignables par `db.<nom>` au moment du découpage.

Il n'interdit pas d'AJOUTER — la surface grandit à chaque lot, et l'y contraindre ferait
un test qu'on met à jour sans le lire. Il interdit de RETIRER : un nom qui disparaît
casse un appelant, et sur une surface plate de ce genre, ça se découvre à l'exécution
d'un chemin peu emprunté — pas au démarrage.

Retirer volontairement un nom reste possible : on retire aussi sa ligne ici, et le
diff dit alors ce qu'on a fait.
"""
from __future__ import annotations

# Inventaire relevé le 13/08, APRÈS la première couture — il contient donc déjà les
# noms qu'elle a produits. Le commentaire d'origine disait « juste avant le découpage »,
# ce qui était faux et rendait ce fichier trompeur : on aurait cru qu'il attestait de
# l'état du monolithe, alors qu'il atteste de l'état d'une étape. Il vaut pour ce qu'il
# est — un cliquet qui n'autorise plus de retrait à partir de ce point.
_SURFACE = """
    Any CONVERT_GUIDES_TO_NODES_SQL CREDENTIAL_PROVIDERS ConnectionPool
    DATASTORE_ROWS_TEXT DOCS_TEXT DocConflict FIELD_VALUE_PARAM_SQL FILE_TEXT
    GOOGLE GUIDES_TEXT INSTR_TEXT Iterable Iterator KEY_PROVIDERS LAYER_KEYS
    LAYER_VALUE_PARAM_SQL MARK_NODES_TO_EMBED_SQL MAX_EXTRACT_ATTEMPTS
    NODES_TEXT NODE_DIRTY_SQL NODE_KIND Optional PROJECTS_TEXT RANKED_SOURCES
    RANK_VECTOR_COLUMN ROW_VALUES_TEXT_SQL SUBSCRIPTION_STATUSES Sequence
    TERMINAL_PAYMENT_STATUSES VALUE_LAYER activation_funnel
    active_subscription_plans add_doc_change_request add_group_disabled_tool
    add_org_disabled_tool add_project_file add_project_link
    add_user_disabled_tool add_user_enabled_tool aggregate_gaps
    aggregate_tool_feedback annotations archive_project aux_embed
    backfill_unipile_member_scope backlinks billing bkey_index_expr
    bound_unipile_account_ids bump_counter cancel_scheduled_email
    claim_due_scheduled_emails clear_account_grant clear_aux_dirty
    clear_connector_access clear_embed_dirty clear_group_connector_access
    clear_member_api_key clear_operated_account clear_operated_pointers_to
    clear_option_comp clear_row_dirty clear_unipile_account
    connector_failure_stats connector_grants consume_upload_token
    contextmanager count_datastore_rows_for_ns count_renewal_attempts
    count_unipile_accounts_for_org counter_sum_today create_api_token
    create_datastore_namespace create_doc create_project
    create_unipile_pending datastore datastore_active_lease
    datastore_aggregate datastore_claim_next datastore_claim_row
    datastore_claimed_rows datastore_count_rows datastore_delete_row
    datastore_drop_column datastore_drop_key_index datastore_embed
    datastore_ensure_key_index datastore_field_values
    datastore_find_row_id_by_key datastore_get_row
    datastore_has_key_index datastore_insert_row datastore_key_dup_groups
    datastore_list_rows datastore_list_rows_after
    datastore_merge_key_duplicates datastore_merge_row_locked
    datastore_namespace_activity datastore_namespaces_with_key
    datastore_offending_enum_values datastore_overlong_fields
    datastore_release_by_run datastore_release_claim datastore_row_activity
    datastore_row_keys datastore_rows_by_ids datastore_update_row
    datastore_upsert_row date datetime dead_unipile_account_ids_for
    delete_api_token delete_datastore_namespace_by_id delete_doc
    delete_google_oauth delete_guide_db delete_project_file
    delete_subscription derive_description dict_row doc_backlinks doc_rev
    due_subscriptions duplicate_project edge_exists edges_for emails
    emails_by_subs enqueue_scheduled_email field_read_sql field_value_sql
    files_pending_extraction find_copied_project finish_run
    get_account_profile get_all_connector_schemas get_aux_embedding_sha
    get_billing_payment_by_ref get_connector_schema get_datastore_namespace
    get_datastore_namespace_by_id get_doc_by_id get_doc_by_public_token
    get_doc_change_request get_doc_embedding_sha get_extracted_text
    get_google_oauth get_guide_db get_init_guide_db get_legal_acceptances
    get_member_api_key get_operated_account get_org_subscription
    get_org_unipile_limit get_platform_instruction get_project_by_id
    get_project_by_mcp_slug get_project_file get_resource_grant
    get_row_embedding_sha get_run get_tool_call get_unipile_account
    get_unipile_account_id get_unipile_feed_synced_at get_usage_today get_user
    get_user_by_email get_users_by_email google grant_resource granted_accounts_for grants
    group_key group_member_allowed_connectors group_restricted_connectors
    guides has_member_api_key has_option_comp hashlib increment_usage
    index_ddl init_db insert_billing_payment insert_grant insert_run
    insert_tool_call insert_usage_signal instruction_usage
    is_comp_subscription is_tool_disabled_for json keys leaf_read_sql legal
    list_account_grants_by_owner list_account_grants_to
    list_all_datastore_namespaces list_all_projects list_api_tokens
    list_billing_payments list_change_requests_by_project
    list_change_requests_by_requester list_connector_access
    list_datastore_namespaces_for_owners list_datastore_namespaces_granted_to
    list_dirty_aux list_dirty_docs list_dirty_rows list_doc_change_requests
    list_doc_revisions list_docs_for_project list_google_accounts
    list_grants_for_user list_group_connector_access list_group_disabled_tools
    list_guides_db list_member_projects list_option_comps
    list_option_comps_for_option list_org_disabled_tools list_org_grants
    list_platform_instructions list_project_activity list_project_files
    list_project_links list_projects_for_owners list_projects_granted_to
    list_published_mcp_projects list_resource_grants list_runs
    list_scheduled_emails list_tenant_issuers list_tool_calls
    call_filter_clauses count_calls_of_org_runs_elsewhere journal_calls
    export_tool_calls_for_org list_unipile_accounts list_unipile_accounts_by_org
    list_unipile_pending_for_sub list_usage_signals list_user_disabled_tools
    list_user_enabled_tools list_users list_users_with_grants
    live_edges_for_grantee log_project_activity logger logging
    mark_cancel_at_period_end mark_scheduled_failed mark_scheduled_sent
    member_allowed_connectors member_instance_suspended
    migrate_business_key_indexes migrate_sub move_doc move_doc_to_project
    open_billing_payments org_adoption org_restricted_connectors
    org_unipile_account_ids os paths platform_instructions
    project_grant_counts project_names project_run_stats project_run_tools
    project_runs project_spine projects prune_tool_calls psycopg
    rank_backfill_sql rank_column_ddl rank_expr rank_pending_counts re
    recent_runs reconcile_tenant_migration record_legal_acceptances
    remove_group_disabled_tool remove_org_disabled_tool remove_project_link
    remove_user_disabled_tool remove_user_enabled_tool
    rename_datastore_namespace_by_id reparent_datastore_namespace
    reparent_project replace_doc_chunk_embeddings resolve_datastore_ns
    resolve_doc_change_request resolve_sub resolve_unipile_pending
    resource_ids_with_edges rest_call_stats
    retry_billing_at revoke_edges revoke_resource_grant save_extracted_text
    schedule_next_billing search search_briefs_semantic
    search_datastore_rows_fts search_datastore_rows_semantic search_docs_fts
    search_docs_in_project search_docs_semantic search_file_contents
    search_files_meta search_guides_fts search_guides_semantic
    search_procedures_fts search_project_briefs seat_binding_elsewhere secrets
    seed_guide_db seed_init_guide_db seed_platform_instruction
    set_account_grant set_avatar_url set_comp_subscription
    set_connector_access set_datastore_schema set_datastore_semantic
    set_default_google_account set_doc_public set_google_oauth
    set_group_connector_access set_guide_db set_init_guide_db
    set_member_api_key set_operated_account set_option_comp
    set_org_unipile_limit set_platform_instruction set_project_file_public
    set_project_mcp_instructions set_project_mcp_publication
    set_subscription_status set_unipile_account set_user_locale set_user_role
    split_layer split_list_path stamp_rank_vector subscription_plan_for_org
    sweep_grace_expired sweep_period_end_cancellations tenants time timezone
    tokens tool_call_stats touch_unipile_feed_synced unipile
    unipile_account_owners update_account_profile update_billing_payment
    update_doc update_google_access_token update_project
    update_project_link_ref upload_tokens upsert_aux_embedding
    upsert_connector_schema upsert_doc_embedding upsert_org_subscription
    upsert_row_embedding upsert_user usage users verify_api_token visibility
""".split()


def test_no_public_name_disappears():
    import oto_mcp.db as db
    presents = {n for n in dir(db) if not n.startswith("_")}
    manquants = sorted(set(_SURFACE) - presents)
    assert not manquants, (
        f"{len(manquants)} nom(s) ont quitté la surface plate `db.<fn>` : "
        f"{', '.join(manquants)} — un déplacement doit rester invisible aux appelants")


def test_the_inventory_is_not_empty():
    """Un inventaire vidé par accident rendrait le test vert et inutile."""
    assert len(_SURFACE) > 300
