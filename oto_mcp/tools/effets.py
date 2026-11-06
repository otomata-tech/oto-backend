"""What a connector tool call DOES — `lire`, `ecrire`, `envoyer` or `detruire` — declared op
by op, with the batch an op that writes or sends may carry.

**Why a declaration**: a tool protection lives in the path EVERY call goes through (a
direct MCP call: `middleware/call_context.py`; `oto_call` and recipes:
`tools/meta.executer_cible`), the same for both (decision of 03/11/2026). It judges what
the call does, and that cannot be guessed from a verb: an `add` that enrolls contacts in a
sequence SENDS, a `set_*` that replaces DESTROYS, an `http_act` request destroys when its
`method` is DELETE. So it is written here, read and reviewed once, rather than inferred at
call time. `tools/lecture.py` declares by WHOLE tool (`LECTURE` / `SIMULATION` / `ACTE`,
for the MCP client); this table goes one level down, by `op`, for the server.

**Why one table, not an annotation next to each tool**: the tools come from ~150 modules
and from the connectors package (described functions registered generically): an
annotation per op would scatter the declaration over files nobody reviews together, while
the classing is a SECURITY review that reads best side by side (`recipes/ecritures.py`,
`copilot/classement.OPS_EN_LECTURE` and `test_convention_act.A_MIGRER` are tables for the
same reason). `tests/test_effets_declares.py` keeps it complete against the catalogue
really mounted; `scripts/amorcer_effets.py` proposes the lines of a new tool or op.

The classes, from least to most constraining (`CLASSES`):

- `lire`: writes NOTHING anywhere (a paid search still reads).
- `ecrire`: creates or updates something, at the provider or in the person's space.
- `envoyer`: something LEAVES towards a third party — a message, an invitation, a
  publication, a campaign or a sequence that will send (contacts added to an active
  sequence, a list that feeds a sending tool), an agent launched.
- `detruire`: deletes, removes, merges, acts in bulk, archives, purges, refunds, voids,
  deactivates, cancels, revokes, or REPLACES without keeping what it replaced (a `set_*`
  that overwrites). An `oto_guide` `write` writes: the version it replaces is kept and
  restorable (`db/guide_revisions.py`). A switch the same person flips back with a
  plain write is not destroyed: `oto_disable_tool` writes (`oto_enable_tool` undoes it).

A relay (`oto_call`) has no class of its own: it takes its target's (`RELAIS`).

A doubt is the most constraining class, never `lire` by default.

**Scope**: every tool the server mounts that is not declared `LECTURE`
(`tools/lecture.en_lecture` is its declaration: all its ops read) — connector tools AND
platform tools (`oto_*`, `data_*`, `run_*`, `email_send`, `feedback`) — plus the
capabilities declared but not mounted yet (feature flag off).

**Nothing reads this table at call time yet**: `effet()` is what the next lots plug into
the common path. No call is refused because of it today.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

CLASSES = ("lire", "ecrire", "envoyer", "detruire")
_RANG = {c: i for i, c in enumerate(CLASSES)}
#: How many records one call may carry when nothing else is declared.
LOT_MAX = 50


@dataclass(frozen=True)
class Lot:
    """The argument that carries a BATCH of records (`contact_ids`, `to`, `items`…), and how
    many one call may carry. `avec`: other arguments counted in the same batch (`cc`,
    `bcc` of a message)."""
    cle: str
    max: int = LOT_MAX
    avec: tuple[str, ...] = ()

    @property
    def cles(self) -> tuple[str, ...]:
        return (self.cle, *self.avec)


class EffetNonDeclare(LookupError):
    """A tool or an op this table does not declare: never presumed to read."""


#: A judge decides the class from the ARGUMENTS when the op alone does not (`http_act`
#: request: its `method`). It receives the op and the arguments and returns a class, or
#: `None` to keep the declared one.
Juge = Callable[[Optional[str], dict], Optional[str]]


def _plus_contraignante(*classes: str) -> str:
    return max(classes, key=_RANG.__getitem__)


# --- judges -----------------------------------------------------------------------------

#: What an HTTP verb does to the org's API: GET reads, POST/PATCH may send anything,
#: PUT replaces, DELETE deletes.
_CLASSE_PAR_METHODE = {"GET": "lire", "HEAD": "lire", "POST": "envoyer", "PATCH": "envoyer",
                       "PUT": "detruire", "DELETE": "detruire"}


def _juge_http_act(op: Optional[str], args: dict) -> Optional[str]:
    """`request` does what its `method` does (none given: the declared doubt). `call` does
    what its SAVED action's method does, read in the database
    (`http_calls.methode_de_l_action`): an unknown action raises, never a default."""
    if op == "request":
        return _CLASSE_PAR_METHODE.get(str(args.get("method") or "").upper())
    if op == "call":
        from .http_calls import methode_de_l_action
        methode = str(methode_de_l_action(args.get("operation"))).upper()
        if methode not in _CLASSE_PAR_METHODE:
            raise EffetNonDeclare(f"`http_act` action `{args.get('operation')}`: "
                                  f"unknown method `{methode}`")
        return _CLASSE_PAR_METHODE[methode]
    return None


JUGES: dict[str, Juge] = {
    "http_act": _juge_http_act,
}
#: The judges that READ THE DATABASE, by tool and op: `effet()` on such a call is
#: synchronous I/O — from an `async def`, run it off the event loop
#: (`await run_in_threadpool(effet, name, args)`), as `lit_la_base()` tells.
JUGES_EN_BASE: dict[str, frozenset[str]] = {
    "http_act": frozenset({"call"}),
}

#: A tool that only RELAYS a call to another (`oto_call`): its effect is its target's,
#: named by these arguments (target name, target arguments). This is its declaration: it
#: has no line in `EFFETS`, and a call that names no target RAISES `EffetNonDeclare` —
#: it reaches nothing, so there is nothing to judge (decision of 03/11/2026).
RELAIS: dict[str, tuple[str, str]] = {
    "oto_call": ("name", "arguments"),
}


def cible(name: str, args: Optional[dict] = None) -> tuple[str, dict]:
    """The tool and arguments a call really reaches: itself, or the target of a relay
    (`oto_call(name=…, arguments=…)`). The caller checks `lecture.en_lecture` on THIS
    tool (and canonicalizes a tenant-prefixed name, as `oto_call` does) before `effet`.
    Raises `EffetNonDeclare` for a relay call that names no target."""
    args = args or {}
    relais = RELAIS.get(name)
    if relais is None:
        return name, args
    nom = args.get(relais[0])
    if not (isinstance(nom, str) and nom):
        raise EffetNonDeclare(f"`{name}` without `{relais[0]}`: it reaches no tool, "
                              "there is no effect to judge")
    cibles = args.get(relais[1])
    return cible(nom, cibles if isinstance(cibles, dict) else {})


def lit_la_base(name: str, args: Optional[dict] = None) -> bool:
    """True when `effet(name, args)` reads the database (a judge in `JUGES_EN_BASE`):
    call it off the event loop then."""
    nom, a = cible(name, args)
    return a.get("op") in JUGES_EN_BASE.get(nom, ())


# --- the declaration --------------------------------------------------------------------

#: Tool → its class (a tool without `op`), or {class: "op op …"} (a tool multiplexed by
#: `op`). Every op the tool serves is here, and nothing else.
EFFETS: dict[str, Union[str, dict[str, str]]] = {
    # update: arrays (emails, organization_ids, person_ids) REPLACE the current set
    "affinity_entity": {"lire": "search get entries relationships", "ecrire": "create", "detruire": "update"},
    "affinity_interactions": "lire",
    "affinity_list": {"lire": "list get fields views options"},
    # set_fields writes the given field values only, no set is replaced
    "affinity_list_entry": {"lire": "query get", "ecrire": "add set_fields", "detruire": "remove"},
    # update: an association list REPLACES that kind ([] clears it)
    "affinity_note": {"lire": "list get", "ecrire": "create", "detruire": "update delete"},
    "ahrefs_account": "lire",
    "ahrefs_act": {"envoyer": "publish_post"},
    "ahrefs_batch_analysis": "lire",
    "ahrefs_brand_radar": "lire",
    "ahrefs_brand_radar_prompt": {"lire": "list", "ecrire": "create"},
    "ahrefs_brand_radar_report": {"lire": "list", "ecrire": "create"},
    "ahrefs_gsc": "lire",
    "ahrefs_keyword_list": {"lire": "list", "ecrire": "add"},
    "ahrefs_keywords_explorer": "lire",
    "ahrefs_locations": "lire",
    "ahrefs_project": {"lire": "list", "ecrire": "create"},
    "ahrefs_project_competitors": {"lire": "list", "ecrire": "add"},
    # tag with update_mode=replace only relabels tracked keywords: stays ecrire
    "ahrefs_project_keywords": {"lire": "list", "ecrire": "add tag"},
    "ahrefs_public": {"lire": "crawler_ips crawler_ip_ranges domain_rating_free domain_rating_top_domains"},
    "ahrefs_rank_tracker": "lire",
    "ahrefs_serp_overview": "lire",
    "ahrefs_site_audit": "lire",
    "ahrefs_site_explorer": "lire",
    "ahrefs_social": {"lire": "channels channel_metrics activity_history authors posts post_metrics"},
    "ahrefs_web_analytics": "lire",
    "aircall_call_ai": {"lire": "transcription summary topics sentiments action_items"},
    "aircall_calls": {"lire": "list search get recording voicemail"},
    "aircall_company": "lire",
    "aircall_contacts": {"lire": "list search get"},
    "aircall_numbers": {"lire": "list get"},
    "aircall_teams": {"lire": "list get"},
    "aircall_users": {"lire": "list get"},
    "airtable_attachment": "ecrire",
    "airtable_base": {"lire": "list whoami", "ecrire": "create"},
    # a row comment is seen by collaborators and @mentions notify them
    "airtable_comment": {"lire": "list", "envoyer": "create update", "detruire": "delete"},
    "airtable_field": {"lire": "list", "ecrire": "create update"},
    # update/upsert: replace=true (PUT) CLEARS every field not sent
    "airtable_record": {"lire": "list get", "ecrire": "create", "detruire": "update upsert delete"},
    # each push REPLACES the synced table content
    "airtable_sync": "detruire",
    "airtable_table": {"lire": "schema", "ecrire": "create update"},
    # add_queue_numbers feeds the Power Dialer; create_queue replaces the current queue;
    # publish_call_flow replaces the live inbound routing at once
    "allo_act": {"ecrire": "create_tag add_tags create_note update_note update_thread create_person update_person create_company update_company create_attribute add_queue update_queue create_summary_template update_call_flow_draft add_receptionist_website", "envoyer": "send_sms create_thread add_comment update_comment add_queue_numbers", "detruire": "update_summary update_conversation delete_tag remove_tag delete_note update_attribute delete_attribute create_queue remove_queue_numbers delete_queue set_queue_assignee update_summary_template delete_summary_template set_default_summary_template delete_webhook publish_call_flow update_receptionist set_receptionist_status set_receptionist_prompt update_receptionist_website delete_receptionist_website delete_receptionist_file"},
    # completing a sequence task moves the lead on to the next (sending) step
    "amplemarket_act": {"ecrire": "create_person_enrichment create_company_enrichment create_email_validation create_lead_list add_lead_list_leads create_contact add_excluded_emails add_excluded_domains review_phone_number create_call_log", "envoyer": "add_sequence_leads", "detruire": "cancel_person_enrichment cancel_company_enrichment cancel_email_validation set_task_completed set_task_skipped set_mailbox_daily_limit remove_excluded_emails remove_excluded_domains"},
    "amplitude_chart": "lire",
    "amplitude_cohort": {"lire": "list members", "ecrire": "request"},
    "amplitude_query": {"lire": "segmentation funnel retention active_users sessions"},
    "amplitude_schema": {"lire": "events event_properties user_properties group_properties volumes"},
    "amplitude_user": {"lire": "search activity"},
    "apify_abort_run": "detruire",
    "apify_google_maps_places": "lire",
    "apify_google_maps_reviews": "lire",
    "apify_indeed_jobs": "lire",
    "apify_linkedin_companies": "lire",
    "apify_linkedin_company_employees": "lire",
    "apify_linkedin_jobs": "lire",
    "apify_linkedin_posts": "lire",
    "apify_linkedin_profiles": "lire",
    # any actor: a run is created at the provider (decided: a write, not a send)
    "apify_run": "ecrire",
    "apify_run_sync": "ecrire",
    "apify_website_contacts": "lire",
    "apify_website_crawl": "lire",
    "apify_welcome_to_the_jungle_jobs": "lire",
    "apollo_act": {"envoyer": "send_email"},
    # a batched match, not a bulk mutation; reveal_phone_number orders an async reveal
    "apollo_bulk_match": "ecrire",
    # update: label_names REPLACES list membership
    "apollo_contact": {"lire": "fields search get", "ecrire": "create_field", "detruire": "update"},
    "apollo_conversation": {"lire": "search get export_status", "ecrire": "export"},
    "apollo_email": {"lire": "status search content stats", "ecrire": "draft"},
    "apollo_email_accounts": "lire",
    "apollo_email_schedules": "lire",
    "apollo_match_person": "lire",
    "apollo_reveal_phone": "ecrire",
    # with `datastore` the numbers are written into the person's table
    "apollo_reveal_phone_result": "ecrire",
    # create with active=true activates; update with active=false deactivates
    "apollo_sequence": {"lire": "search", "envoyer": "create activate", "detruire": "update deactivate archive"},
    # update_status: mark_as_finished / remove / stop takes people out of a sequence
    "apollo_sequence_contacts": {"lire": "activity", "envoyer": "add", "detruire": "update_status"},
    "ashby_add_note": "ecrire",
    "ashby_application": "lire",
    "ashby_applications": "lire",
    "ashby_candidate": "lire",
    "ashby_candidates": "lire",
    "ashby_job": "lire",
    "ashby_jobs": "lire",
    "ashby_search_candidates": "lire",
    "attio_attribute": {"lire": "list get options statuses", "ecrire": "create create_option create_status"},
    "attio_comment": {"lire": "threads thread get", "envoyer": "create", "detruire": "delete"},
    # update: overwrite_multiselect=true REPLACES multiselect values
    "attio_entry": {"lire": "query get", "ecrire": "create", "detruire": "update delete"},
    # update may change the list's access controls (revoke)
    "attio_list": {"lire": "list get views", "ecrire": "create", "detruire": "update"},
    "attio_meeting": {"lire": "list get recordings recording transcript"},
    "attio_note": {"lire": "list get", "ecrire": "create", "detruire": "delete"},
    "attio_object": {"lire": "list get views"},
    # update: overwrite_multiselect=true REPLACES multiselect values
    "attio_record": {"lire": "list get search", "ecrire": "create", "detruire": "update merge delete"},
    "attio_task": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    "attio_workspace_member": {"lire": "list get"},
    "bigquery_catalog": "lire",
    "bigquery_query": "lire",
    "bigquery_results": "lire",
    "bigquery_table": "lire",
    "bls_oews_wages": "lire",
    "boondmanager_create": "ecrire",
    "boondmanager_dictionary": "lire",
    "boondmanager_get": "lire",
    "boondmanager_search": "lire",
    "brevo_account": "lire",
    "brevo_campaign": {"lire": "list report ab_test", "ecrire": "create update", "envoyer": "test"},
    # upsert with list_ids subscribes to sending lists; update unsubscribes or blacklists
    "brevo_contact": {"lire": "list get stats attributes", "envoyer": "upsert", "detruire": "update"},
    "brevo_crm_create": "ecrire",
    "brevo_crm_get": "lire",
    # unlink_contact_ids / unlink_ids detach linked records
    "brevo_crm_link": "detruire",
    "brevo_crm_list": "lire",
    "brevo_crm_meta": "lire",
    "brevo_crm_update": "ecrire",
    "brevo_export_contacts": "ecrire",
    # list_ids / new_list put the contacts on sending lists
    "brevo_import_contacts": "envoyer",
    # add puts contacts on a list that feeds campaigns
    "brevo_list": {"lire": "list get contacts folders segments", "ecrire": "create update", "envoyer": "add", "detruire": "remove"},
    "brevo_send_email": "envoyer",
    "brevo_template": {"lire": "list", "ecrire": "create update"},
    "brevo_transactional": {"lire": "logs content events report blocked blocked_domains"},
    # status: active starts sending, paused/draft deactivates
    "brevoauto_automation": {"lire": "list get catalog", "ecrire": "create", "detruire": "status"},
    "brevoauto_connect_start": "ecrire",
    "brevoauto_connect_status": "lire",
    "brevoauto_step": {"ecrire": "add configure", "detruire": "delete"},
    "brevoauto_trigger": {"ecrire": "add configure", "detruire": "delete"},
    "browser_connect_start": "ecrire",
    "browser_connect_status": "lire",
    # arbitrary JS on a logged-in session: may POST or DELETE anything
    "browser_eval": "detruire",
    "browser_fetch": "lire",
    "browser_sites": "lire",
    "calendar_calendars": "lire",
    # create with attendees invites them; update: attendees REPLACES the guest list
    "calendar_event": {"lire": "list get", "envoyer": "create", "detruire": "update rm"},
    "ccn_article": {"lire": "search get"},
    "ccn_conventions": "lire",
    "chat_act": {"envoyer": "send"},
    "claap_recording_views": {"lire": "list get"},
    "claap_recordings": {"lire": "list get transcript", "ecrire": "create", "detruire": "delete"},
    "clay_account": "lire",
    "clay_get_run": "lire",
    "clay_list_tables": "lire",
    # each new row triggers the table's automations
    "clay_push_rows": "envoyer",
    # a routine run at the provider (decided: a write)
    "clay_run_routine": "ecrire",
    "clay_search": "lire",
    "clay_search_fields": "lire",
    "clay_search_next": "lire",
    "clay_search_reference": "lire",
    "clay_tables_query": "lire",
    "cloro_ask": "lire",
    "cloro_google": {"lire": "serp news"},
    "cognism_enrich_account": "lire",
    "cognism_enrich_contact": "lire",
    "cognism_entitlement": {"lire": "contact account"},
    "cognism_filter_values": "lire",
    "cognism_redeem": {"lire": "contact account"},
    "cognism_search": {"lire": "contact account"},
    "crunchbase_connect_start": "ecrire",
    "crunchbase_connect_status": "lire",
    "crunchbase_get_company": "lire",
    "crunchbase_get_funding_rounds": "lire",
    "crunchbase_get_person": "lire",
    "crunchbase_search_companies": "lire",
    "crunchbase_search_people": "lire",
    "culture_spectacle_export_url": "lire",
    "culture_spectacle_get": "lire",
    "culture_spectacle_search": "lire",
    "culture_spectacle_stats": "lire",
    "dataforseo_account": "lire",
    "dataforseo_ai_visibility": "lire",
    "dataforseo_backlinks": "lire",
    "dataforseo_domain": "lire",
    "dataforseo_keywords": "lire",
    # crawl_start persists a billed crawl task at the provider; every other op reads
    "dataforseo_site_audit": "ecrire",
    "dataforseo_web_mentions": "lire",
    # granting shares (Google notifies by default), `remove` revokes: judged on email/remove
    "drive_access": "detruire",
    "drive_file": {"lire": "list metadata download export", "ecrire": "upload create_folder update", "detruire": "delete"},
    "dropcontact_credits": "lire",
    "dropcontact_enrich": "ecrire",
    "dropcontact_result": "lire",
    "elevenlabs_act": {"ecrire": "create_speech create_music create_sound create_voice_design create_transcript"},
    # add_member grants access to the group's team and site
    "entra_act": {"ecrire": "add_license enable_account", "envoyer": "add_member", "detruire": "remove_license disable_account remove_member"},
    "figma_file_meta": "lire",
    "figma_get_comments": "lire",
    "figma_get_file": "lire",
    "figma_get_images": "lire",
    "figma_post_comment": "envoyer",
    "finkare_debtor": {"lire": "list get invoices score", "ecrire": "create update"},
    # create hands a receivable to collection (dunning of a real debtor); bulk is a batch
    "finkare_invoice": {"lire": "list", "envoyer": "create", "detruire": "bulk cancel"},
    "finkare_payment": {"lire": "list get by_invoice stats"},
    # trigger acts on a real debtor: start/resume/escalate send, pause/cancel stop
    "finkare_workflow": {"lire": "status history next_action stats", "detruire": "trigger"},
    "firecrawl_cancel_crawl": "detruire",
    # starts an async crawl job persisted at the provider
    "firecrawl_crawl": "ecrire",
    "firecrawl_crawl_status": "lire",
    # starts an async extraction job persisted at the provider
    "firecrawl_extract": "ecrire",
    "firecrawl_extract_status": "lire",
    "firecrawl_map": "lire",
    "firecrawl_scrape": "lire",
    "firecrawl_search": "lire",
    "fireflies_askfred": {"lire": "list_threads get_thread", "ecrire": "create_thread continue_thread", "detruire": "delete_thread"},
    "fireflies_bite": {"lire": "get list", "ecrire": "create"},
    "fireflies_channel": {"lire": "get list"},
    # add_bot joins a third-party meeting as a visible participant
    "fireflies_live_meeting": {"lire": "list_active list_action_items", "ecrire": "create_action_item create_soundbite update_state", "envoyer": "add_bot"},
    "fireflies_org": {"lire": "analytics audit_events contacts apps rule_executions"},
    # upload attendees may get the recap; update_privacy can widen access
    "fireflies_transcript": {"lire": "list get", "ecrire": "update_title update_channel create_upload_url confirm_upload", "envoyer": "upload update_privacy share", "detruire": "delete unshare"},
    "fireflies_user": {"lire": "get list groups", "detruire": "set_role"},
    # update can make a group public (workspace-wide access); update_member overwrites a role
    "folk_group": {"lire": "list custom_fields get_custom_field members", "ecrire": "create create_custom_field update_custom_field", "envoyer": "update add_member", "detruire": "remove_member update_member"},
    # update with remove_from_groups detaches records from groups
    "folk_record": {"lire": "search get", "ecrire": "create add_to_group mark_done mark_todo", "detruire": "update delete"},
    "folk_user": {"lire": "list get"},
    # update can replace the subscribed events or redirect the target URL
    # registers a webhook: data then leaves to its URL (decided: a send)
    "folk_webhook": {"lire": "list", "envoyer": "create", "detruire": "update"},
    "foncier_beges": "lire",
    "foncier_comparables_app": "lire",
    "foncier_conso_elec": "lire",
    "foncier_dpe": {"lire": "adresse stats tertiaire"},
    "foncier_dvf": {"lire": "prix_m2 comparables comparables_adresse"},
    "foncier_geocode": "lire",
    "foncier_icpe": {"lire": "installations emissions"},
    "foncier_isochrone": "lire",
    "foncier_permis_search": "lire",
    "foncier_prix_m2_app": "lire",
    "foncier_proprietaire": "lire",
    "foncier_site": {"lire": "parcelle bati solaire adresse"},
    "foncier_site_app": "lire",
    "forager_account": {"lire": "me balance_log balance_totals"},
    "forager_autocomplete": {"lire": "industries organizations organization_keywords locations person_skills web_technologies"},
    "forager_feedback": {"ecrire": "personal_email phone_number work_email"},
    "forager_job_post": {"lire": "search totals"},
    "forager_organization": {"lire": "search totals website"},
    "forager_person": {"lire": "detail reverse_by_email reverse_by_phone work_emails personal_emails phone_numbers role_search role_search_totals"},
    "frenchtech_appels": "lire",
    "frenchtech_evenements": "lire",
    "frenchtech_financements": "lire",
    "frenchtech_ftc_scenarios": "lire",
    "frenchtech_get_annuaire": "lire",
    "frenchtech_membres": "lire",
    "frenchtech_search_annuaire": "lire",
    "fullenrich_enrich_linkedin": "ecrire",
    "fullenrich_result": "lire",
    "ga4_key_events": "lire",
    "ga4_metadata": "lire",
    "ga4_properties": "lire",
    "ga4_realtime": "lire",
    "ga4_report": "lire",
    "github_act": {"ecrire": "create_branch"},
    # dispatch/rerun trigger real runs (builds, deployments)
    "github_actions": {"lire": "workflows workflow runs run jobs job logs artifacts artifact download", "envoyer": "dispatch rerun rerun_failed", "detruire": "cancel delete_run delete_artifact"},
    # write over an existing file (with sha) overwrites it
    "github_files": {"lire": "read list readme", "detruire": "write delete"},
    # creating an issue notifies watchers; update with labels/assignees replaces them
    "github_issues": {"lire": "search get comments labels milestones", "ecrire": "add_labels create_label lock unlock create_milestone", "envoyer": "create comment update_comment assign", "detruire": "update delete_comment set_labels remove_label unassign"},
    # add_team_member/add_collaborator send an invitation and grant access
    "github_orgs": {"lire": "me get members teams team_members my_orgs rate_limit is_member membership team team_repos collaborators is_collaborator permission", "envoyer": "add_team_member add_collaborator", "detruire": "set_membership remove_member remove_team_member remove_collaborator"},
    # a PR or a submitted review notifies; merge is irreversible
    "github_pulls": {"lire": "search get files commits merged reviews review_comments reviewers", "ecrire": "update update_branch", "envoyer": "create review submit_review review_comment request_review", "detruire": "merge remove_reviewers"},
    # a release is a publication that notifies watchers
    "github_repos": {"lire": "get tags mine org user branches commits commit compare contributors languages topics releases release latest_release", "envoyer": "create_release update_release"},
    "github_search": {"lire": "users repos code issues commits"},
    "gmail": {"lire": "search get accounts attachment drafts", "ecrire": "draft"},
    "gmail_act": {"envoyer": "send", "detruire": "archive trash"},
    "gocardless_creditors": "lire",
    "gocardless_events": "lire",
    "gocardless_failed": "lire",
    "gocardless_failure_reason": "lire",
    "gocardless_payment": "lire",
    "gocardless_payment_party": "lire",
    "gocardless_payments": "lire",
    "gocardless_payout": "lire",
    "gocardless_payouts": "lire",
    "google_accounts": "lire",
    "google_ads_customers": "lire",
    "google_ads_fields": "lire",
    "google_ads_search": "lire",
    "gr_lookup": "lire",
    # registers a webhook: data then leaves to its URL (decided: a send)
    "grain_hook": {"lire": "list", "envoyer": "create", "detruire": "delete"},
    "grain_org": {"lire": "users teams meeting_types"},
    "grain_recording": {"lire": "list get", "ecrire": "update tag", "envoyer": "share_user share_team", "detruire": "untag unshare_user unshare_team"},
    "grain_recording_file": {"lire": "download", "ecrire": "create_upload_url"},
    "grain_transcript": "lire",
    "granola_content": {"lire": "list_notes get_note get_transcript list_folders"},
    # update can disable the endpoint or replace its scopes/events
    # registers a webhook: data then leaves to its URL (decided: a send)
    "granola_webhook_endpoint": {"lire": "list", "envoyer": "create", "detruire": "update delete"},
    "greenhouse_application": {"lire": "list get"},
    "greenhouse_candidate": {"lire": "list get", "ecrire": "create add_note"},
    "greenhouse_job": {"lire": "list get"},
    "greenhouse_users": "lire",
    "hellostock_demande": {"lire": "list get"},
    "hellostock_demande_send": "envoyer",
    "hellostock_demande_set_status": "detruire",
    # keywords REPLACES the whole public list
    "hellostock_offre_update": "detruire",
    "hellostock_membre": {"lire": "list get"},
    "hellostock_offre": {"lire": "list get"},
    "hellostock_positionnements": "lire",
    "higgsfield_act": {"ecrire": "create_generation create_upload", "detruire": "cancel delete"},
    "hithorizons_company": "lire",
    "hithorizons_search_company": "lire",
    "hithorizons_search_unstructured": "lire",
    "hithorizons_suggestions": "lire",
    # import only proposes candidate actions; call runs a saved action of any verb
    "http_act": {"lire": "import", "detruire": "call request set_definition"},
    "http_doc": "lire",
    "http_post": "envoyer",
    # add_members may also remove (remove_record_ids); copy_from moves up to 100 000 records
    "hubspot_list": {"lire": "search get members record_lists", "ecrire": "create restore", "detruire": "update delete add_members remove_members clear_members copy_from"},
    "hubspot_object": {"lire": "search list get associations", "ecrire": "create update add_note", "detruire": "delete merge"},
    "hubspot_owners": "lire",
    # update PATCHes the raw definition: options given replace the whole option list
    "hubspot_property": {"lire": "list get groups", "ecrire": "create", "detruire": "update delete"},
    "hubspot_push_rows": "envoyer",
    "hunter_domain_search": "lire",
    "hunter_email_finder": "lire",
    "hunter_email_verify": "lire",
    "infosec_domain": {"lire": "whois dns email_security subdomains tls headers blocklist deliverability"},
    "inqom_balance": "lire",
    "inqom_company": "lire",
    "inqom_document": "lire",
    "inqom_dossier": {"lire": "list get"},
    # not wired upstream yet: declared as the write it stands for
    "inqom_entry_create": "ecrire",
    "inqom_entry_line": {"lire": "list count"},
    "inqom_ref": "lire",
    "instagram_act": {"envoyer": "send"},
    "instagram_meta_get_account_insights": "lire",
    "instagram_meta_get_best_hours": "lire",
    "instagram_meta_get_media_insights": "lire",
    "instagram_meta_get_profile": "lire",
    "instagram_meta_get_recent_media": "lire",
    "jev_ask": "lire",
    "jev_items": "lire",
    "jev_job": {"lire": "status", "detruire": "cancel"},
    # writes answers into the table; overwrite=true replaces rows already decided
    "jev_rows": "detruire",
    "juris_decision": {"lire": "search get"},
    "kaspr_enrich_linkedin": "ecrire",
    # subscribe starts the list's flows or emails a double opt-in confirmation
    "klaviyo_consent": {"envoyer": "subscribe", "detruire": "unsubscribe"},
    # create starts the flows triggered by the metric
    "klaviyo_events": {"lire": "list", "envoyer": "create"},
    # add starts the "Added to List" flows
    "klaviyo_lists": {"lire": "list get members", "envoyer": "add", "detruire": "remove"},
    "klaviyo_profiles": {"lire": "list get", "ecrire": "upsert"},
    "leexi_calls": {"lire": "search get", "ecrire": "create presign"},
    # create with to_record sends the assistant into the meeting; launch_bot with stop_task removes one
    "leexi_meetings": {"lire": "list get", "envoyer": "create", "detruire": "launch_bot delete"},
    # update REPLACES the text of a language
    "leexi_notes": {"lire": "list get", "detruire": "update delete"},
    # update can deactivate (fields active=false)
    "leexi_teams": {"lire": "list get", "ecrire": "create", "detruire": "update delete"},
    "leexi_users": {"lire": "list get", "ecrire": "create", "detruire": "update deactivate"},
    "lemlist_act": {"ecrire": "create_persona", "envoyer": "send_email send_linkedin send_whatsapp", "detruire": "delete_persona"},
    "lemlist_add_lead_variables": "ecrire",
    # export_email mails the export to any address; update can replace the senders
    "lemlist_campaign": {"lire": "statutes reports batch_stats export_status export_leads", "ecrire": "create pause duplicate export_start", "envoyer": "export_email", "detruire": "update"},
    # arming auto-review makes every lead added send at once
    "lemlist_campaign_auto_review": "envoyer",
    "lemlist_campaign_start": "envoyer",
    "lemlist_company": {"lire": "list notes", "ecrire": "upsert note_create", "detruire": "delete"},
    # list_manage removes contacts with action="remove"
    "lemlist_contact": {"lire": "list get lists list_export", "ecrire": "upsert list_create", "detruire": "delete list_manage"},
    "lemlist_create_lead": "envoyer",
    "lemlist_delete_activity_transcript": "detruire",
    "lemlist_deliverability": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    # webhook_url posts the result to any URL
    "lemlist_enrich": "envoyer",
    # not a bulk_* op: one async enrichment per person; webhook_url posts the results to any URL
    "lemlist_enrich_bulk": "envoyer",
    "lemlist_enrich_lead": "envoyer",
    # messages can mark as read; labels_attach with append=false replaces the labels
    "lemlist_inbox": {"lire": "list labels label_get drafts draft_get", "ecrire": "messages label_create draft_create draft_update", "detruire": "labels_attach labels_remove draft_delete"},
    "lemlist_launch_lead": "envoyer",
    # import_crm loads a whole CRM selection into a campaign, unbounded
    "lemlist_lead": {"lire": "get list", "ecrire": "update pause interested not_interested vars_update upload_audio", "envoyer": "resume", "detruire": "delete unsubscribe vars_delete import_crm"},
    # lemwarm_start sends inside the warm-up network
    "lemlist_mailbox": {"lire": "lemwarm", "ecrire": "connect test lemwarm_update lemwarm_pause", "envoyer": "lemwarm_start", "detruire": "disconnect"},
    "lemlist_push_rows": "envoyer",
    "lemlist_schedule": {"lire": "list get for_campaign", "ecrire": "create update associate", "detruire": "delete"},
    # ab_winner sends the winning template to every remaining lead
    "lemlist_sequence": {"lire": "get ab_get", "ecrire": "add_step update_step ab_create ab_update", "envoyer": "ab_winner", "detruire": "delete_step ab_delete"},
    "lemlist_task": {"lire": "list", "ecrire": "create update ignore"},
    "lemlist_team": {"lire": "team credits senders user user_channels crm_users crm_filters fields"},
    # add, var_add and contact_add only add to a do-not-contact list
    "lemlist_unsubscribe": {"lire": "list get export var_list var_get var_export contact_status contact_export", "ecrire": "add var_add contact_add", "detruire": "delete var_bulk var_remove contact_remove"},
    # create/update with push_to_campaign and activate feed a campaign on their own
    "lemlist_watchlist": {"lire": "list filters filter_values library history signals", "envoyer": "create update push_signals", "detruire": "delete"},
    # registers a webhook: data then leaves to its URL (decided: a send)
    "lemlist_webhook": {"lire": "list", "envoyer": "add", "detruire": "delete"},
    "lever_add_candidate": "ecrire",
    "lever_add_note": "ecrire",
    "lever_opportunities": "lire",
    "lever_opportunity": "lire",
    "lever_posting": "lire",
    "lever_postings": "lire",
    "lever_stages": "lire",
    "lever_users": "lire",
    "lightfield_accounts": {"lire": "search get definitions", "ecrire": "upsert"},
    "lightfield_act": {"envoyer": "send_email"},
    "lightfield_contacts": {"lire": "search get definitions", "ecrire": "upsert"},
    "lightfield_emails": {"lire": "search get", "ecrire": "draft"},
    "lightfield_lists": {"lire": "list get members", "ecrire": "upsert"},
    "lightfield_meetings": {"lire": "search get definitions"},
    "lightfield_notes": {"lire": "definitions", "ecrire": "create"},
    "lightfield_objects": {"lire": "list definitions"},
    "lightfield_opportunities": {"lire": "search get definitions", "ecrire": "upsert"},
    "lightfield_tasks": {"lire": "definitions", "ecrire": "upsert"},
    "lighton_ask": "lire",
    "lighton_delete_document": "detruire",
    "lighton_extract": "lire",
    "lighton_files": "lire",
    "lighton_parse": "lire",
    "lighton_search": "lire",
    "lighton_upload_document": "ecrire",
    "lighton_workspaces": "lire",
    # a new comment notifies the issue's subscribers
    "linear_comment": {"lire": "list get", "ecrire": "update", "envoyer": "create", "detruire": "delete"},
    "linear_cycle": {"lire": "list get"},
    "linear_issue": {"lire": "list get search", "ecrire": "create update", "detruire": "archive delete"},
    "linear_label": {"lire": "list get", "ecrire": "create", "detruire": "delete"},
    "linear_project": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    "linear_team": {"lire": "list get states"},
    "linear_user": {"lire": "list get viewer"},
    # registers a webhook: data then leaves to its URL (decided: a send)
    "linear_webhook": {"lire": "list", "envoyer": "create update", "detruire": "delete"},
    "linkedin_unipile_account": {"lire": "status contracts inmail_balance", "ecrire": "select"},
    # update can archive a thread; update_member can reject an applicant
    "linkedin_unipile_act": {"envoyer": "send react_message publish_post comment_post react_post invite accept_invitation endorse", "detruire": "update update_member decline_invitation cancel_invitation"},
    "linkedin_unipile_facets": "lire",
    "linkedin_unipile_job": {"lire": "postings posting applicants applicant projects"},
    "linkedin_unipile_search": "lire",
    "loi_article": {"lire": "get search versions"},
    "loi_codes": "lire",
    "lucca_absence": {"lire": "list get"},
    "lucca_department": {"lire": "list get"},
    "lucca_employee": {"lire": "list get"},
    "lucca_establishment": "lire",
    "lucca_expense_claim": "lire",
    "lucca_leave_request": {"lire": "list get"},
    "luma_act": {"envoyer": "send_blast"},
    # update before sending can change who receives a scheduled blast
    "luma_blasts": {"lire": "list get", "envoyer": "update", "detruire": "delete"},
    # add_admins gives a person control of the whole calendar
    "luma_calendar": {"lire": "me get admins event_tags places images org_admins org_calendars", "ecrire": "update create_event_tag update_event_tag tag_events upload_url create_calendar", "envoyer": "add_admins", "detruire": "delete_event_tag untag_events"},
    # import and restore put people on the newsletter audience
    "luma_contacts": {"lire": "list tags", "ecrire": "create_tag update_tag tag", "envoyer": "import restore", "detruire": "block remove delete_tag untag"},
    # update notifies guests unless suppressed; update_host overwrites an access level
    "luma_event_admin": {"ecrire": "create submit approve", "envoyer": "update add_host transfer", "detruire": "cancel reject update_host remove_host"},
    # add registers people and may email them
    "luma_guest_admin": {"envoyer": "add invite", "detruire": "set_status set_tickets"},
    # add may charge the person (paid tier)
    "luma_memberships": {"lire": "tiers", "envoyer": "add", "detruire": "set_status"},
    "luma_tickets": {"lire": "types type coupons", "ecrire": "create_type update_type create_coupon update_coupon", "detruire": "delete_type"},
    # registers a webhook: data then leaves to its URL (decided: a send)
    "luma_webhooks": {"lire": "list get", "envoyer": "create update", "detruire": "delete"},
    "lusha_search_and_enrich": "lire",
    "mailpool_account": {"lire": "slots warmup warmup_inbox warmup_available"},
    # Rewrites the whole DNS record set, removes records, replaces a CNAME (dry_run=True previews).
    "mailpool_domain_fix": "detruire",
    "mailpool_domains": {"lire": "list get dns audit availability suggestions google_workspace_check microsoft_365_check owners"},
    "mailpool_mailboxes": {"lire": "list get"},
    # `run` sends a real test email from the mailbox.
    "mailpool_spam_checks": {"lire": "get list", "envoyer": "run", "detruire": "delete"},
    "make_get_scenario": "lire",
    "make_get_scenario_blueprint": "lire",
    "make_list_organizations": "lire",
    "make_list_scenario_logs": "lire",
    "make_list_scenarios": "lire",
    "make_list_teams": "lire",
    # Triggers an external automation.
    "make_run_scenario": "envoyer",
    "meta_ads_accounts": "lire",
    # mode="async" creates a report run at Meta.
    "meta_ads_insights": "ecrire",
    "meta_ads_objects": "lire",
    "microsoft_accounts": "lire",
    "microsoft_admin_consent": "lire",
    "minari_analytics": {"lire": "overview users lists objections"},
    "minari_call": {"lire": "list get transcript recording"},
    # `add` pushes contacts into a rep's dialer queue: they will be called.
    "minari_contact": {"envoyer": "add", "detruire": "remove"},
    "minari_custom_field": {"lire": "list", "ecrire": "create", "detruire": "delete"},
    # `create` pushes a prospecting list into a rep's dialer queue.
    "minari_list": {"lire": "list get", "envoyer": "create", "detruire": "delete"},
    "minari_user": "lire",
    "modjo_ask": "lire",
    "modjo_calls": {"lire": "list get transcript summaries notes next_steps recording crm_answers tags", "ecrire": "add_tag upload", "detruire": "remove_tag"},
    "monid_endpoint": {"lire": "discover inspect"},
    # generic paid gateway to third-party endpoints (decided: a write)
    "monid_run": "ecrire",
    # `stop` cancels a running run.
    "monid_runs": {"lire": "list get", "detruire": "stop"},
    "monid_wallet": "lire",
    "mux_act": {"ecrire": "create_upload create_asset"},
    "n8n_activate_workflow": "envoyer",
    "n8n_deactivate_workflow": "detruire",
    "n8n_get_execution": "lire",
    "n8n_get_workflow": "lire",
    "n8n_list_executions": "lire",
    "n8n_list_tags": "lire",
    "n8n_list_workflows": "lire",
    # update may email/SMS the patient (send_appointment_modified_*); reschedule has no such flag.
    "nextmotion_appointment": {"lire": "list get", "envoyer": "update reschedule", "detruire": "delete"},
    "nextmotion_availability": "lire",
    # create of an appointment_request may notify the requesting person.
    "nextmotion_calendar": {"lire": "list get", "ecrire": "update", "envoyer": "create", "detruire": "delete"},
    # items/distributions/post_treatment read; update replaces sub_visit_types/pricings; update_post_treatment configures patient emails.
    "nextmotion_catalog": {"lire": "list get items distributions post_treatment", "ecrire": "create reorder add_item assign_distribution", "envoyer": "update_post_treatment", "detruire": "update delete set_items set_distributions unassign_distribution"},
    "nextmotion_clinic": "lire",
    # kind="message" sends an email/SMS/WhatsApp to the patient.
    "nextmotion_communication": {"envoyer": "create"},
    "nextmotion_device_usage": "lire",
    # pay may fire send_reminder_on_invoice_paid; credit_note voids/refunds (payments, do_validate).
    "nextmotion_invoice": {"lire": "list get", "ecrire": "update validate", "envoyer": "pay", "detruire": "credit_note"},
    "nextmotion_journey": "lire",
    "nextmotion_lead": {"lire": "list get", "ecrire": "create update convert", "detruire": "delete"},
    "nextmotion_patient": {"lire": "list get", "ecrire": "create update"},
    "nextmotion_patient_demographics": "lire",
    "nextmotion_package_usage": "lire",
    "nextmotion_patient_stats": "lire",
    "nextmotion_payment": {"lire": "list get", "ecrire": "update"},
    # create of a practitioner with an email may invite them to the clinic account.
    "nextmotion_practitioner": {"lire": "list get", "ecrire": "update", "envoyer": "create", "detruire": "delete"},
    "nextmotion_product": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    "nextmotion_quote": {"lire": "list get", "ecrire": "update validate", "detruire": "delete"},
    # placeholders reads; update writes template and form bodies whole (overwrite).
    # create may register a webhook setting: data then leaves to its URL (decided: a send)
    "nextmotion_setting": {"lire": "list get placeholders", "ecrire": "duplicate", "envoyer": "create", "detruire": "update delete"},
    "nextmotion_statistics": "lire",
    "notion_add_comment": "envoyer",
    "notion_append_blocks": "ecrire",
    "notion_create_database": "ecrire",
    "notion_create_page": "ecrire",
    "notion_delete_block": "detruire",
    # delete=true deletes the comment.
    "notion_edit_comment": "detruire",
    # new_content / allow_deleting_content overwrite the page.
    "notion_edit_markdown": "detruire",
    "notion_get_blocks": "lire",
    "notion_get_comments": "lire",
    "notion_get_database": "lire",
    "notion_get_markdown": "lire",
    "notion_get_page": "lire",
    "notion_list_users": "lire",
    "notion_move_page": "ecrire",
    "notion_query_database": "lire",
    "notion_search": "lire",
    "notion_update_block": "ecrire",
    # in_trash trashes the database; a property set to null drops a column.
    "notion_update_database": "detruire",
    # in_trash / archived trash the page.
    "notion_update_page": "detruire",
    "notion_view": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    # update_page with action="replace" overwrites an element.
    "onenote_act": {"ecrire": "create_page create_section create_notebook", "detruire": "update_page"},
    "onet_occupation": {"lire": "search get"},
    # The agentic brief may enrol people into an EXISTING (possibly active) campaign.
    "origami_campaign_create": "envoyer",
    "origami_campaign_delete": "detruire",
    "origami_campaign_launch": "envoyer",
    # Pausing halts sequences (disable).
    "origami_campaign_pause": "detruire",
    "origami_campaign_resume": "envoyer",
    "origami_campaigns": {"lire": "list_for_table get stats people"},
    # A table feeds its campaigns (auto top-up, sequence columns): new rows may get sent to.
    "origami_rows": {"lire": "list", "envoyer": "upsert"},
    "origami_run_get": "lire",
    "origami_sequences": "lire",
    "origami_tables": {"lire": "list get columns"},
    "origami_upload_csv": "ecrire",
    "origami_workspaces": {"lire": "list", "ecrire": "create"},
    "osm_pois": "lire",
    "outlook": {"lire": "search get attachment drafts folders", "ecrire": "draft"},
    # move can target deleteditems / junkemail.
    "outlook_act": {"envoyer": "send", "detruire": "move archive trash"},
    "outlook_calendar_calendars": "lire",
    # Attendees are emailed by Microsoft; update REPLACES the attendee list.
    "outlook_calendar_event": {"lire": "list get", "envoyer": "create", "detruire": "update rm"},
    "payfit_absence": {"lire": "list", "ecrire": "create", "detruire": "cancel"},
    # create_collaborator with invite_collaborator=True emails PayFit's invitation.
    "payfit_act": {"ecrire": "create_contract create_absence", "envoyer": "create_collaborator request_regularization", "detruire": "cancel_absence set_affiliation"},
    "payfit_collaborator": {"lire": "list get", "ecrire": "create"},
    "payfit_company": "lire",
    "payfit_contract": {"lire": "list get", "ecrire": "create"},
    "payfit_document": {"lire": "income_tax auto_enrolment download"},
    "payfit_insurance": {"lire": "list", "ecrire": "affiliate regularize"},
    "payfit_meal_voucher": "lire",
    "payfit_payslip": {"lire": "list download overtime"},
    "payfit_worked_time": "lire",
    "pennylane_act": {"envoyer": "send_invoice"},
    "pennylane_customer": {"lire": "list find", "ecrire": "create update"},
    "pennylane_firm_companies": "lire",
    "pennylane_firm_create_folder": "ecrire",
    "pennylane_firm_tree": "lire",
    "pennylane_firm_upload_url": "ecrire",
    "pennylane_firm_upload_urls": "ecrire",
    # categorize REPLACES every analytical category.
    "pennylane_invoice": {"lire": "list find categories", "ecrire": "create credit_note update finalize", "detruire": "delete categorize"},
    # update can destroy lines; categorize replaces.
    "pennylane_ledger_entry": {"lire": "list get lines lettered categories", "ecrire": "create", "detruire": "update categorize"},
    # set adds a lettering (reversible); unset removes it.
    "pennylane_ledger_lettering": {"ecrire": "set", "detruire": "unset"},
    # Writes a reconciliation link.
    "pennylane_match": "ecrire",
    # set_status overwrites the quote status.
    "pennylane_quote": {"lire": "list get lines pdf", "ecrire": "create to_invoice", "detruire": "set_status"},
    "pennylane_ref": "lire",
    "pennylane_supplier": {"lire": "list", "ecrire": "create"},
    # update may delete lines (invoice_lines.delete); categorize replaces.
    "pennylane_supplier_invoice": {"lire": "list get lines categories", "ecrire": "import validate", "detruire": "update categorize"},
    "pennylane_transactions": "lire",
    "pennylane_trial_balance": "lire",
    "pennylane_upload_file": "ecrire",
    "pennylaneged_companies": "lire",
    "pennylaneged_company": "lire",
    "pennylaneged_connect_start": "ecrire",
    "pennylaneged_connect_status": "lire",
    "pennylaneged_create_folder": "ecrire",
    "pennylaneged_delete": "detruire",
    "pennylaneged_finalize": "ecrire",
    "pennylaneged_request_upload": "ecrire",
    "pennylaneged_tree": "lire",
    "phantombuster_container_output": "lire",
    "phantombuster_container_results": "lire",
    "phantombuster_get_agent": "lire",
    "phantombuster_get_container": "lire",
    # starts an external automation run (may message people on the user's behalf)
    "phantombuster_launch_agent": "envoyer",
    "phantombuster_list_containers": "lire",
    "pipedrive_lead": {"lire": "list", "ecrire": "create"},
    "pipedrive_note": {"lire": "list", "ecrire": "create"},
    "pipedrive_record": {"lire": "list get fields", "ecrire": "create update", "detruire": "delete"},
    "pipedrive_search": {"lire": "entity all"},
    "pipedrive_users": "lire",
    "planity_get_appointment": "lire",
    "planity_get_best_customers": "lire",
    "planity_get_customer": "lire",
    "planity_get_customer_frequencies": "lire",
    "planity_get_customer_receipts": "lire",
    "planity_get_customer_stats": "lire",
    "planity_get_daily_revenue": "lire",
    "planity_get_new_customers": "lire",
    "planity_get_occupancy_rate": "lire",
    "planity_get_pos_period": "lire",
    "planity_get_receipt": "lire",
    "planity_get_revenue_breakdown": "lire",
    "planity_get_revenue_by_payment_method": "lire",
    "planity_get_revenue_by_vat": "lire",
    "planity_get_revenue_summary": "lire",
    "planity_get_reviews_stats": "lire",
    "planity_get_salon_info": "lire",
    "planity_get_seller_stats": "lire",
    "planity_get_service_stats": "lire",
    "planity_list_appointments": "lire",
    "planity_list_employees": "lire",
    "planity_list_mass_stock_removals": "lire",
    "planity_list_payment_methods": "lire",
    "planity_list_pos_periods": "lire",
    "planity_list_product_orders": "lire",
    "planity_list_products": "lire",
    "planity_list_recurring_appointments": "lire",
    "planity_list_salons": "lire",
    "planity_list_services": "lire",
    "planity_list_stock_movements": "lire",
    "planity_list_suppliers": "lire",
    "planity_search_customers": "lire",
    "posthog_flag": {"lire": "list get experiments experiment"},
    "posthog_group": {"lire": "list find types"},
    "posthog_insight": {"lire": "list get run dashboards dashboard"},
    "posthog_person": {"lire": "list get activity cohorts cohort_persons"},
    # annotate drops a marker on shared charts: additive, no recipient, kept as a write
    "posthog_project": {"lire": "list current annotations", "ecrire": "annotate"},
    "posthog_query": "lire",
    "posthog_recording": {"lire": "list get"},
    "posthog_schema": {"lire": "events properties tables columns values"},
    "productlane_act": {"envoyer": "send_message broadcast_changelog"},
    # create/update may set published=true, which publishes the changelog page
    "productlane_changelogs": {"lire": "search get tags", "ecrire": "create_tag update_tag", "envoyer": "create update", "detruire": "delete delete_tag"},
    "productlane_companies": {"lire": "search get linear_options", "ecrire": "create update", "detruire": "delete merge"},
    # update may set is_subscribed=false (unsubscribe); block silently cuts a sender off
    "productlane_contacts": {"lire": "search get companies issues projects blocked", "ecrire": "create add_company unblock", "detruire": "update delete remove_company block"},
    # create may publish a public article; update may drop images; accept may apply a delete draft
    "productlane_docs": {"lire": "articles article groups drafts draft", "ecrire": "move create_group update_group create_draft", "envoyer": "create", "detruire": "update delete delete_group accept decline"},
    "productlane_roadmap": {"lire": "statuses projects project issues issue workflows", "ecrire": "create_project update_project create_issue update_issue", "detruire": "delete_project delete_issue"},
    "productlane_tags": {"lire": "list get groups group", "ecrire": "create update create_group update_group", "detruire": "delete delete_group"},
    # update with tag_ids replaces the tag set: minor labels, kept as a write
    "productlane_threads": {"lire": "search get messages comments", "ecrire": "create update link", "envoyer": "comment update_comment", "detruire": "delete delete_comment"},
    "productlane_workspace": {"lire": "me folders roadmap portal instances snippets snippet", "ecrire": "create_snippet update_snippet import_file", "detruire": "delete_snippet"},
    "promptwatch_actions": {"lire": "list", "ecrire": "update"},
    "promptwatch_ads": {"lire": "list_ads list_prompts list_domains domain_analytics"},
    "promptwatch_brand": {"lire": "list", "ecrire": "create update"},
    "promptwatch_citation": {"lire": "analytics rank_analysis domains_over_time domains_by_llm grouped llm_sources self_frequency top_pages"},
    "promptwatch_content": {"lire": "list get gap_stats query_fanouts gap_prompts gap_latest gap_recommendations", "ecrire": "create"},
    # update_settings can enable auto-publish or disable the agent; accept_slot queues a publication
    "promptwatch_content_agent": {"lire": "get_settings list_slots get_slot", "ecrire": "update_slot", "envoyer": "accept_slot publish_slot_now", "detruire": "update_settings decline_slot"},
    "promptwatch_models": "lire",
    "promptwatch_monitor": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    "promptwatch_page_tracker": {"lire": "list get prompts responses", "ecrire": "add", "detruire": "delete"},
    "promptwatch_persona": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    "promptwatch_project": "lire",
    # activate only resumes tracking (no recipient), kept as a write
    "promptwatch_prompt": {"lire": "list get", "ecrire": "create update attach_tags attach_topics activate", "detruire": "delete bulk_create bulk_delete deactivate bulk_attach_tags bulk_attach_topics"},
    # set records a live URL and push_draft a CMS draft: writes; publish_live publishes
    "promptwatch_publishing": {"lire": "list_connections status", "ecrire": "set push_draft", "envoyer": "publish_live", "detruire": "clear"},
    "promptwatch_response": {"lire": "list get summary sentiment_distribution sentiment_time_series mentions_time_series top_competitors"},
    "promptwatch_shopping": {"lire": "list_appearances get_item position_analytics list_tracked products_over_time top_merchants top_products", "ecrire": "add_tracked update_tracked", "detruire": "delete_tracked"},
    "promptwatch_sitemap": {"lire": "list_urls progress health"},
    "promptwatch_social": {"lire": "reddit youtube"},
    "promptwatch_taxonomy": {"lire": "list_tags list_topics", "ecrire": "create_tags rename_tag create_topics rename_topic", "detruire": "delete_tag delete_topic"},
    "promptwatch_visibility": {"lire": "time_series sentiment_time_series prompt_time_series competitor_heatmap"},
    "recruitee_add_note": "ecrire",
    "recruitee_candidate": "lire",
    "recruitee_candidates": "lire",
    "recruitee_create_candidate": "ecrire",
    "recruitee_offer": "lire",
    "recruitee_offers": "lire",
    "reddit_post": "lire",
    "reddit_search": "lire",
    "reddit_search_subreddits": "lire",
    "reddit_subreddit": "lire",
    # segments feed Broadcasts; upsert/update may set the global opt-out (unsubscribe)
    "resend_contact": {"lire": "list get", "envoyer": "add_to_segment", "detruire": "upsert update delete remove_from_segment"},
    "resend_segment": {"lire": "list get", "ecrire": "create"},
    "routine_fire": "envoyer",
    "salesforce_connect": {"ecrire": "start"},
    "salesforce_describe": "lire",
    "salesforce_note": {"lire": "list", "ecrire": "create"},
    "salesforce_query": {"lire": "soql sosl"},
    "salesforce_record": {"lire": "list get", "ecrire": "create update upsert", "detruire": "delete bulk_create bulk_update"},
    "sante_essms_dimensions": "lire",
    "sante_essms_search": "lire",
    "sante_finess": "lire",
    "sante_finess_search": "lire",
    "searchapi_search": "lire",
    "sellsy_contact": {"lire": "list search get companies", "ecrire": "create update custom_fields", "detruire": "delete"},
    # validate is irreversible (freezes the invoice number); status may cancel or refuse a quote
    "sellsy_document": {"lire": "list search get payments linked", "ecrire": "create update custom_fields", "detruire": "delete validate status"},
    "sellsy_item": {"lire": "list search get prices", "ecrire": "create update custom_fields", "detruire": "delete"},
    "sellsy_opportunity": {"lire": "list search get", "ecrire": "create update move custom_fields", "detruire": "delete"},
    "sellsy_payment": {"lire": "list search get custom_fields", "detruire": "delete"},
    "sellsy_ref": "lire",
    "sellsy_search": "lire",
    "sellsy_task": {"lire": "list search get", "ecrire": "create update custom_fields", "detruire": "delete"},
    # custom_fields writes when data is given
    "sellsy_third_party": {"lire": "list search get contacts", "ecrire": "create update convert link_contact record_payment custom_fields", "detruire": "delete unlink_contact"},
    "serpapi_google_finance": "lire",
    "serpapi_google_flights": "lire",
    "serpapi_google_hotels": "lire",
    "serpapi_google_trends": "lire",
    "serpapi_jobs": {"lire": "search details"},
    "serpapi_search": "lire",
    # writing cells is a write (decided), even over existing values
    "sharepoint_excel_act": {"ecrire": "update_range"},
    # conflict='replace' overwrites an existing file or folder
    "sharepoint_file": {"lire": "list get search download", "detruire": "upload create_folder"},
    "sharepoint_site": {"lire": "search get drives"},
    "sheets_create": "ecrire",
    # writing cells is a write (decided), even over existing values; clear empties a range
    "sheets_spreadsheet": {"lire": "metadata read", "ecrire": "write add_sheet rename_sheet number_format copy_sheet format column_width freeze dropdown", "detruire": "clear delete_sheet delete_rows"},
    "signwell_account": {"lire": "me api_application", "detruire": "delete_api_application"},
    "signwell_act": {"envoyer": "send_document remind_document"},
    # create sends one signature request per CSV row: a mass send with no countable list
    "signwell_bulk_send": {"lire": "list get documents csv_template validate_csv", "detruire": "create"},
    # create sends immediately when draft=false; update_recipients re-notifies signers
    "signwell_document": {"lire": "get completed_pdf nom151_certificate", "ecrire": "update_authentication", "envoyer": "create update_recipients", "detruire": "delete"},
    # create_document sends immediately when draft=false
    "signwell_template": {"lire": "get", "ecrire": "create update", "envoyer": "create_document", "detruire": "delete"},
    # registers a webhook: data then leaves to its URL (decided: a send)
    "signwell_webhook": {"lire": "list", "envoyer": "create", "detruire": "delete"},
    "silae_declaration": {"lire": "dsn_list dsn_content states"},
    "silae_dossier": {"lire": "list numbers collective_agreements current_period establishments organisms"},
    "silae_employee": {"lire": "list get jobs from_internal_id"},
    "silae_payslip": {"lire": "pdf_ids indices header lines details totals"},
    "silae_report": {"lire": "generate status", "ecrire": "start"},
    "silae_variables_to_enter": "lire",
    "sivan_absences": "lire",
    "sivan_badged_events": "lire",
    "sivan_companies": "lire",
    "sivan_events": "lire",
    "sivan_salaries": "lire",
    "sivan_timesheets": "lire",
    "sivan_timesheets_summary": "lire",
    # update_usergroup replaces the default channel list: minor, kept as a write
    "slack_act": {"ecrire": "create_usergroup update_usergroup enable_usergroup add_usergroup_members", "detruire": "disable_usergroup set_usergroup_members remove_usergroup_members"},
    # a reaction is visible to the channel
    "slack_add_reaction": "envoyer",
    "slack_delete_message": "detruire",
    "slack_download_file": "lire",
    "slack_find_user_by_email": "lire",
    # joining posts a membership event visible to the channel
    "slack_join_channel": "ecrire",
    "slack_list_channels": "lire",
    # conversations.open creates the DM channel if missing (no message sent)
    "slack_open_dm": "ecrire",
    "slack_post_message": "envoyer",
    "slack_read_history": "lire",
    "slack_read_thread": "lire",
    "snitcher_contact": {"lire": "list", "ecrire": "reveal_email"},
    # set/set_many write one organisation's field values, nothing wider
    "snitcher_custom_field": {"lire": "list get values", "ecrire": "create update set set_many", "detruire": "delete clear"},
    "snitcher_organisation": {"lire": "list search get", "ecrire": "tag", "detruire": "untag"},
    "snitcher_session": "lire",
    "snitcher_workspace": {"lire": "list get me segments", "ecrire": "create update create_tag", "envoyer": "invite", "detruire": "delete"},
    "spott_application": {"lire": "list", "ecrire": "create move"},
    "spott_candidate": {"lire": "list get search", "ecrire": "create update"},
    "spott_client": {"lire": "list get search contacts"},
    "spott_job": {"lire": "list get search"},
    "spott_note": {"lire": "list", "ecrire": "create"},
    "spott_people": "lire",
    "spott_placements": "lire",
    "spott_stages": "lire",
    "spott_users": "lire",
    # update_product/update_price/update_promotion_code take active=false (retire/revoke)
    "stripe_catalog": {"lire": "list_products get_product list_prices get_price list_coupons get_coupon list_promotion_codes get_promotion_code", "ecrire": "create_product create_price create_coupon update_coupon create_promotion_code", "detruire": "update_product update_price update_promotion_code"},
    # update_link takes active=false (deactivates the payment link)
    "stripe_checkout": {"lire": "list_links get_link link_line_items list_sessions get_session session_line_items", "ecrire": "create_link", "detruire": "update_link"},
    "stripe_customer": {"lire": "list get payment_methods", "ecrire": "create update"},
    "stripe_invoice": {"lire": "list get lines totals list_items", "ecrire": "create_draft add_item update"},
    "supabase_auth_config": "lire",
    "supabase_list_projects": "lire",
    "supabase_query_logs": "lire",
    "tally_account": {"lire": "me users invites", "envoyer": "invite", "detruire": "remove_user cancel_invite"},
    "tally_analytics": {"lire": "metrics visits submissions dimensions drop_off"},
    # create may set status=PUBLISHED (live form); update/update_blocks replace the whole block array
    "tally_form": {"lire": "list get questions blocks", "ecrire": "update_question", "envoyer": "create", "detruire": "update update_blocks delete"},
    "tally_submission": {"lire": "list get", "detruire": "delete"},
    # retry fires a real delivery to the endpoint
    # registers a webhook: data then leaves to its URL (decided: a send)
    "tally_webhook": {"lire": "list events", "envoyer": "create update retry", "detruire": "delete"},
    "tally_workspace": {"lire": "list get folders", "ecrire": "create update create_folder update_folder", "detruire": "delete delete_folder"},
    # lists by default, but `create` creates a task list
    "tasks_lists": "ecrire",
    # set_status only toggles done/undone, reversible
    "tasks_task": {"lire": "list get", "ecrire": "upsert set_status", "detruire": "rm"},
    "tavily_crawl": "lire",
    "tavily_extract": "lire",
    "tavily_map": "lire",
    "tavily_search": "lire",
    "teams_act": {"envoyer": "post reply"},
    "teamtailor_candidate": "lire",
    "teamtailor_candidates": "lire",
    "teamtailor_create_candidate": "ecrire",
    "teamtailor_job": "lire",
    "teamtailor_job_applications": "lire",
    "teamtailor_jobs": "lire",
    "telegram_act": {"envoyer": "send"},
    "threecx_call": {"lire": "list export"},
    # Live on the PBX: an update REPLACES the routing a public number or queue had (the
    # preview shows it, nothing keeps it), a navigation list is replaced whole, and an
    # action may act in bulk. `dry_run=True`, the default, only previews.
    "threecx_act": {"ecrire": "create", "detruire": "update delete call"},
    "threecx_recording": "lire",
    "topograph_company": "lire",
    "topograph_search": "lire",
    "transcription_create": "ecrire",
    "transcription_status": "lire",
    "typeform_delete_responses": "detruire",
    # create is public by default; update can publish or unpublish via is_public
    "typeform_forms": {"lire": "list get", "envoyer": "create", "detruire": "update replace delete"},
    # upsert replaces an existing webhook whole (secret may be dropped)
    "typeform_webhooks": {"lire": "list get", "detruire": "upsert delete"},
    "ubersuggest_account": {"lire": "status limits locations location_details validate_site blog"},
    "ubersuggest_backlinks": {"lire": "overview list anchors linking_domains opportunity page_shares"},
    "ubersuggest_domain": {"lire": "overview keywords top_pages top_countries competitors traffic_value page_overview page_keywords"},
    "ubersuggest_keyword_lists": {"lire": "list get", "ecrire": "create add rename", "detruire": "remove"},
    "ubersuggest_keywords": {"lire": "overview suggestions match google serp metric content_ideas estimate_clicks"},
    "ubersuggest_projects": {"lire": "list get positions opportunities business_summary brand_config brand_overview brand_prompts article_titles get_article", "ecrire": "create add_keywords add_competitors generate_article"},
    "ubersuggest_site_audit": {"lire": "status results pages pagespeed", "ecrire": "start"},
    "unipile_connect_start": "ecrire",
    "urba_annuaire": {"lire": "services maires presidents_epci"},
    "urba_argiles": "lire",
    "urba_epfif": "lire",
    "urba_iris": "lire",
    "urba_lieu": "lire",
    "urba_qpv": "lire",
    "urba_qpv_proximite": "lire",
    "urba_reglement": "lire",
    "urba_risques": "lire",
    "urba_socio": "lire",
    "urba_zonage": "lire",
    "waalaxy_campaign": {"lire": "list"},
    # add with campaign_id enrols prospects in an outreach campaign
    "waalaxy_prospect": {"envoyer": "add"},
    "waalaxy_prospect_list": {"lire": "list"},
    "waterfall_company_act": {"ecrire": "update_rows"},
    "waterfall_email_act": {"ecrire": "update_rows"},
    "waterfall_people_act": {"ecrire": "update_rows"},
    "waterfall_person_act": {"ecrire": "update_rows"},
    "waterfall_phone_act": {"ecrire": "update_rows"},
    "waterfall_tech_act": {"ecrire": "update_rows"},
    "web_image": "lire",
    "webflow_cms": {"lire": "site collections collection items item", "ecrire": "create update", "detruire": "delete"},
    "webflow_forms": {"lire": "list get"},
    "webflow_pages": {"lire": "list get content", "ecrire": "update"},
    "webflow_publish": "envoyer",
    "webflow_site_publish": "envoyer",
    "webflow_submissions": {"lire": "list get", "ecrire": "update", "detruire": "delete"},
    # registers a webhook: data then leaves to its URL (decided: a send)
    "webflow_webhooks": {"lire": "list get", "envoyer": "create", "detruire": "delete"},
    "whatsapp_act": {"envoyer": "send"},
    "wordpress_article": "ecrire",
    "wordpress_content": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    "wordpress_media": {"lire": "list get", "ecrire": "upload update", "detruire": "delete"},
    "wordpress_publish": {"envoyer": "publish", "detruire": "unpublish"},
    "wordpress_site": "lire",
    "wordpress_terms": {"lire": "list get", "ecrire": "create update", "detruire": "delete"},
    # update can archive the candidate (archived=true)
    "wttj_candidate": {"lire": "list get resume", "ecrire": "create", "detruire": "update"},
    "wttj_comment": "envoyer",
    "wttj_emails": "lire",
    "wttj_job": {"lire": "list get"},
    "wttj_moves": "lire",
    "wttj_organization": "lire",
    # composes a draft request; nothing is sent
    "yousign_creer": "ecrire",
    "yousign_document_signe": "lire",
    "yousign_envoyer": "envoyer",
    "yousign_statut": "lire",
    # runs an external automation
    "zapier_execute_action": "envoyer",
    "zapier_execution_log": "lire",
    "zapier_list_actions": "lire",
    "zerobounce_credits": "lire",
    "zerobounce_verify_batch": "lire",
    "zerobounce_verify_email": "lire",
    "zoho_connect": {"lire": "modes", "ecrire": "start"},
    "zoho_modules": "lire",
    "zoho_note": {"lire": "list", "ecrire": "create"},
    "zoho_record": {"lire": "list get search", "ecrire": "create update", "detruire": "delete"},
    "zohoanalytics_export": "lire",
    "zohoanalytics_orgs": "lire",
    "zohoanalytics_query": "lire",
    "zohoanalytics_view_details": "lire",
    "zohoanalytics_views": "lire",
    "zohoanalytics_workspaces": "lire",
    "zohodesk_article": "lire",
    "zohodesk_articles": "lire",
    "zohodesk_contacts": "lire",
    "zohodesk_create_contact": "ecrire",
    "zohodesk_create_ticket": "ecrire",
    "zohodesk_departments": "lire",
    "zohodesk_search_articles": "lire",
    "zohodesk_search_tickets": "lire",
    "zohodesk_ticket": "lire",
    "zohodesk_ticket_threads": "lire",
    "zohodesk_tickets": "lire",
    "zohodesk_update_ticket": "ecrire",
    "zohosign_act": {"envoyer": "send_from_template remind", "detruire": "cancel"},
    # --- platform tools (oto_*, data_*, run_*, email_send, feedback): not connectors, but
    # every direct MCP call goes through the same path. Inviting, granting, sharing or
    # lending gives a person access: `envoyer`; revoking, removing, replacing: `detruire`.
    "data_aggregate": "lire",
    "data_app": "lire",
    # takes a lease on the next row: a write
    "data_claim_next": "ecrire",
    "data_create_datastore": "ecrire",
    "data_delete_datastore": "detruire",
    "data_delete_row": "detruire",
    "data_drop_column": "detruire",
    "data_get_schema": "lire",
    "data_list_datastores": "lire",
    # `remove` drops columns
    "data_patch_schema": "detruire",
    "data_release": "ecrire",
    "data_rename_datastore": "ecrire",
    # the card writes the status the user clicks
    "data_review_app": "ecrire",
    "data_row_comment": "ecrire",
    "data_row_comments": "lire",
    "data_row_history": "lire",
    "data_rows": "lire",
    # replaces the whole schema
    "data_set_schema": "detruire",
    "data_share": "envoyer",
    # updates every row a filter matches: no batch can bound it
    "data_update_where": "detruire",
    "data_url": "lire",
    "data_write": "ecrire",
    "email_send": "envoyer",
    "feedback": "ecrire",
    "oto_account_access": {"lire": "list", "envoyer": "grant", "detruire": "revoke"},
    "oto_admin_access_shadow": {"lire": "read"},
    "oto_admin_account": {"ecrire": "resume", "detruire": "suspend"},
    "oto_admin_agent_repair": {"lire": "list get diagnose", "ecrire": "close", "envoyer": "rerun escalate"},
    # dark-launched capabilities (feature flag): declared before they mount
    "oto_admin_cancel_contract": "detruire",
    # free-string `op`
    "oto_admin_connector_setting": {"lire": "list", "ecrire": "reload", "detruire": "set clear"},
    "oto_admin_guide": {"lire": "get list", "detruire": "set delete"},
    "oto_admin_guides_semis": "lire",
    "oto_admin_instance_health": "lire",
    "oto_admin_invite": {"lire": "list", "envoyer": "create", "detruire": "revoke"},
    "oto_admin_key_grant": {"lire": "list", "envoyer": "grant", "detruire": "revoke"},
    "oto_admin_legal_proof": "lire",
    "oto_admin_model_spend": "lire",
    "oto_admin_monitoring": {"lire": "summary rest rest_calls connectors transport funnel calls call runs run gaps tool_quality agents_en_echec connecteurs_orphelins"},
    "oto_admin_org": {"lire": "list get entitlements", "ecrire": "create", "detruire": "archive"},
    "oto_admin_org_member": {"lire": "list connectors events", "envoyer": "add", "detruire": "remove set_role"},
    "oto_admin_platform_balances": "lire",
    "oto_admin_platform_instructions": {"lire": "list get drift", "detruire": "set"},
    "oto_admin_set_contract": "detruire",
    "oto_admin_set_option": "detruire",
    "oto_admin_set_plan": "detruire",
    "oto_admin_signal": {"lire": "list notify_preview", "ecrire": "set_status reroute", "envoyer": "notify_send"},
    "oto_admin_tenant": {"lire": "list get keys admins org_grants", "ecrire": "reload enable", "envoyer": "admin_add org_grant", "detruire": "key_clear admin_remove org_revoke disable"},
    "oto_admin_unipile_seat": {"lire": "list", "detruire": "release"},
    "oto_admin_user": {"lire": "list get", "detruire": "set_role"},
    "oto_admin_vault_health": "lire",
    "oto_apps": {"lire": "list get revisions validate read_file", "ecrire": "create update patch unarchive file", "detruire": "archive restore unfile put_file"},
    "oto_clear_group": "ecrire",
    "oto_clear_org": "ecrire",
    "oto_clear_project": "ecrire",
    # recommend REPLACES the org's kit
    "oto_connector": {"lire": "list setup_link", "ecrire": "select force", "detruire": "pause unselect recommend"},
    "oto_connector_access_level": {"lire": "get", "detruire": "set clear"},
    "oto_connector_activation": {"lire": "list", "detruire": "set clear"},
    "oto_context": "lire",
    # reversible by oto_enable_tool
    "oto_disable_tool": "ecrire",
    "oto_doc": {"lire": "list search get revisions backlinks shared_with_me outline verify", "ecrire": "create update patch move unarchive", "envoyer": "set_public", "detruire": "bulk_create delete revert archive"},
    "oto_doc_app": "lire",
    "oto_connector_app": "lire",
    "oto_enable_tool": "ecrire",
    "oto_fleet": {"lire": "list get state", "ecrire": "create update take beat ack_stop", "envoyer": "launch", "detruire": "stop"},
    # `run` executes stored pure code (no network, no secret) and stores its files
    "oto_function": {"lire": "list get versions", "ecrire": "create propose run test", "envoyer": "publish", "detruire": "refuse"},
    "oto_group": {"lire": "list get list_invites", "ecrire": "create", "envoyer": "add_member invite", "detruire": "remove_member set_instruction"},
    # write/patch/restore REPLACE the body but KEEP the version they replace, restorable
    # (`db/guide_revisions.py`); delete stays destructive: the guide leaves the catalogue
    # and every reader, even though its last version is kept
    "oto_guide": {"lire": "list read revisions", "ecrire": "write patch restore", "detruire": "delete"},
    "oto_guide_v2": {"lire": "list read revisions", "ecrire": "write patch restore", "detruire": "delete"},
    "oto_identity": {"lire": "list", "ecrire": "rename", "detruire": "set"},
    "oto_import": "ecrire",
    "oto_instance": {"lire": "list verify", "envoyer": "lend"},
    "oto_list_my_tools": {"lire": "list search"},
    "oto_list_orgs": "lire",
    "oto_node": "lire",
    "oto_node_edit": {"ecrire": "create update move", "detruire": "delete"},
    "oto_node_rows": "lire",
    "oto_org": {"lire": "list_invites list_loans", "ecrire": "create update accept_invite", "envoyer": "invite", "detruire": "archive reject_invite"},
    "oto_org_monitoring": {"lire": "summary calls call connectors adoption runs run gaps tool_quality signals export agents_en_echec"},
    "oto_org_settings": {"lire": "get preview", "detruire": "set"},
    "oto_procedure": {"lire": "get list describe library_list library_get share_get share_readers", "ecrire": "create rename unarchive fork", "envoyer": "publish share_publish", "detruire": "set archive delete unpublish share_unpublish share_set"},
    "oto_profile": {"lire": "get", "ecrire": "update"},
    "oto_project": {"lire": "list list_templates get activity runs inventory lint", "ecrire": "create update unarchive copy link rank_link", "envoyer": "handoff publish_mcp", "detruire": "archive unlink unpublish_mcp"},
    "oto_project_files": {"lire": "list", "detruire": "delete"},
    # `run` writes rows into a table through declared readers
    "oto_recipe": {"lire": "list get versions describe sample test", "ecrire": "create propose run", "envoyer": "publish"},
    # transfer hands ownership away
    "oto_resource": {"lire": "list get", "envoyer": "share", "detruire": "transfer unshare"},
    "oto_resource_v2": {"lire": "list get", "envoyer": "share", "detruire": "transfer unshare"},
    "oto_run_thread": {"lire": "read", "ecrire": "append"},
    "oto_scheduled_emails": {"lire": "list", "detruire": "cancel"},
    "oto_search": "lire",
    "oto_shell": "lire",
    "oto_tool_schema": "lire",
    # a trigger launches agents later; connect_source registers a webhook at the sender;
    # take_over/give move an agent's ownership; rotations revoke the previous secret
    "oto_trigger": {"lire": "list get deliveries shares links graph", "ecrire": "update link", "envoyer": "create run_now rerun share connect_source lend", "detruire": "delete rotate_secret rotate_address clear_queue take_over give unshare disconnect_source unlink unlend default_sharing"},
    "oto_upload_url": {"ecrire": "create update"},
    "oto_use_group": "ecrire",
    "oto_use_org": "ecrire",
    "oto_use_project": "ecrire",
    "oto_whoami": "lire",
    "run_finish": "ecrire",
    "run_start": "ecrire",
}

#: `tool:op` (or `tool` without op) → the batch it may carry. Every op that writes or sends
#: on a tool with a list argument either declares its batch here or the tool declares
#: that list as not a batch (`PAS_UN_LOT`).
LOTS: dict[str, Lot] = {
    "lemlist_contact:list_create": Lot("contact_ids"),
    "lemlist_inbox:messages": Lot("cc"),
    "lemlist_inbox:label_create": Lot("cc"),
    "lemlist_inbox:draft_update": Lot("cc"),
    "lemlist_act:create_persona": Lot("cc"),
    "lemlist_act:send_linkedin": Lot("cc"),
    "lemlist_act:send_whatsapp": Lot("cc"),
    "lemlist_unsubscribe:var_add": Lot("values"),
    "lemlist_unsubscribe:contact_add": Lot("values"),
    "ubersuggest_keyword_lists:rename": Lot("keywords"),
    "luma_contacts:create_tag": Lot("emails", avec=("user_ids",)),
    "luma_contacts:update_tag": Lot("emails", avec=("user_ids",)),
    "luma_contacts:restore": Lot("emails", avec=("user_ids", "contacts")),
    "luma_calendar:update": Lot("emails", avec=("event_ids",)),
    "luma_calendar:create_event_tag": Lot("event_ids"),
    "luma_calendar:update_event_tag": Lot("event_ids"),
    "luma_calendar:upload_url": Lot("emails", avec=("event_ids",)),
    "luma_calendar:create_calendar": Lot("emails", avec=("event_ids",)),
    "affinity_list_entry:add": Lot("items", max=25),
    "affinity_list_entry:set_fields": Lot("items", max=25),
    "ahrefs_act:publish_post": Lot("channel_ids"),
    "ahrefs_brand_radar_prompt:create": Lot("prompts"),
    "ahrefs_keyword_list:add": Lot("keywords"),
    "ahrefs_project_competitors:add": Lot("competitors"),
    "ahrefs_project_keywords:add": Lot("keywords"),
    "ahrefs_project_keywords:tag": Lot("keywords"),
    "airtable_record:create": Lot("records"),
    "allo_act:create_tag": Lot("entries"),
    "allo_act:add_tags": Lot("entries"),
    "allo_act:create_note": Lot("entries"),
    "allo_act:update_note": Lot("entries"),
    "allo_act:update_thread": Lot("entries"),
    "allo_act:create_person": Lot("entries"),
    "allo_act:update_person": Lot("entries"),
    "allo_act:create_company": Lot("entries"),
    "allo_act:update_company": Lot("entries"),
    "allo_act:create_attribute": Lot("entries"),
    "allo_act:add_queue": Lot("entries"),
    "allo_act:update_queue": Lot("entries"),
    "allo_act:create_summary_template": Lot("entries"),
    "allo_act:update_call_flow_draft": Lot("entries"),
    "allo_act:add_receptionist_website": Lot("entries"),
    "allo_act:send_sms": Lot("entries"),
    "allo_act:create_thread": Lot("entries"),
    "allo_act:add_comment": Lot("entries"),
    "allo_act:update_comment": Lot("entries"),
    "allo_act:add_queue_numbers": Lot("entries"),
    "amplemarket_act:create_person_enrichment": Lot("leads"),
    "amplemarket_act:create_company_enrichment": Lot("companies"),
    "amplemarket_act:create_email_validation": Lot("emails"),
    "amplemarket_act:create_lead_list": Lot("leads"),
    "amplemarket_act:add_lead_list_leads": Lot("leads"),
    "amplemarket_act:create_contact": Lot("leads"),
    "amplemarket_act:add_excluded_emails": Lot("emails"),
    "amplemarket_act:add_excluded_domains": Lot("domains"),
    "amplemarket_act:review_phone_number": Lot("leads"),
    "amplemarket_act:create_call_log": Lot("leads"),
    "amplemarket_act:add_sequence_leads": Lot("leads"),
    "apollo_bulk_match": Lot("people", max=10),
    "apollo_email:draft": Lot("recipients"),
    "apollo_sequence_contacts:add": Lot("contact_ids"),
    "brevo_campaign:create": Lot("email_to"),
    "brevo_campaign:update": Lot("email_to"),
    "brevo_campaign:test": Lot("email_to"),
    "brevo_import_contacts": Lot("contacts"),
    "brevo_list:create": Lot("emails", avec=("ids",)),
    "brevo_list:update": Lot("emails", avec=("ids",)),
    "brevo_list:add": Lot("emails", avec=("ids",)),
    "brevo_send_email": Lot("to", avec=("cc", "bcc")),
    "calendar_event:create": Lot("attendees"),
    "clay_push_rows": Lot("rows"),
    "clay_run_routine": Lot("items"),
    "dropcontact_enrich": Lot("contacts"),
    "fireflies_live_meeting:add_bot": Lot("attendees"),
    "fireflies_live_meeting:create_action_item": Lot("attendees"),
    "fireflies_live_meeting:create_soundbite": Lot("attendees"),
    "fireflies_live_meeting:update_state": Lot("attendees"),
    "fireflies_transcript:upload": Lot("attendees"),
    "fireflies_transcript:update_title": Lot("attendees"),
    "fireflies_transcript:update_privacy": Lot("attendees"),
    "fireflies_transcript:create_upload_url": Lot("attendees"),
    "fireflies_transcript:confirm_upload": Lot("attendees"),
    "fireflies_transcript:update_channel": Lot("transcript_ids", max=5),
    "fireflies_transcript:share": Lot("emails"),
    "folk_record:create": Lot("items"),
    "folk_record:add_to_group": Lot("ids"),
    "folk_record:mark_done": Lot("ids"),
    "folk_record:mark_todo": Lot("ids"),
    "fullenrich_enrich_linkedin": Lot("contacts"),
    "github_issues:assign": Lot("assignees"),
    "github_issues:add_labels": Lot("assignees"),
    "github_issues:create_label": Lot("assignees"),
    "github_issues:lock": Lot("assignees"),
    "github_issues:unlock": Lot("assignees"),
    "github_issues:create_milestone": Lot("assignees"),
    "github_issues:create": Lot("assignees"),
    "github_issues:comment": Lot("assignees"),
    "github_issues:update_comment": Lot("assignees"),
    "github_pulls:request_review": Lot("reviewers", avec=("team_reviewers",)),
    "github_pulls:update": Lot("reviewers", avec=("team_reviewers",)),
    "github_pulls:update_branch": Lot("reviewers", avec=("team_reviewers",)),
    "github_pulls:create": Lot("reviewers", avec=("team_reviewers",)),
    "github_pulls:review": Lot("reviewers", avec=("team_reviewers",)),
    "github_pulls:submit_review": Lot("reviewers", avec=("team_reviewers",)),
    "github_pulls:review_comment": Lot("reviewers", avec=("team_reviewers",)),
    "hellostock_demande_send": Lot("user_ids"),
    "hubspot_list:create": Lot("record_ids"),
    "hubspot_list:restore": Lot("record_ids"),
    "hubspot_push_rows": Lot("row_ids"),
    "inqom_entry_create": Lot("entries"),
    "klaviyo_consent:subscribe": Lot("profiles"),
    "klaviyo_lists:add": Lot("profile_ids"),
    "lemlist_act:send_email": Lot("cc"),
    "lemlist_contact:upsert": Lot("contact_ids"),
    "lemlist_enrich_bulk": Lot("people"),
    "lemlist_inbox:draft_create": Lot("cc"),
    "lemlist_push_rows": Lot("row_ids"),
    "lemlist_task:create": Lot("ids"),
    "lemlist_task:update": Lot("ids"),
    "lemlist_task:ignore": Lot("ids"),
    "lemlist_unsubscribe:add": Lot("values"),
    "lightfield_act:send_email": Lot("to", avec=("cc", "bcc")),
    "lightfield_emails:draft": Lot("to", avec=("cc", "bcc")),
    "luma_calendar:add_admins": Lot("emails"),
    "luma_calendar:tag_events": Lot("event_ids"),
    "luma_contacts:import": Lot("contacts"),
    "luma_contacts:tag": Lot("emails", avec=("user_ids",)),
    "luma_guest_admin:add": Lot("guests"),
    "luma_guest_admin:invite": Lot("guests"),
    "minari_contact:add": Lot("contacts"),
    "minari_list:create": Lot("contacts"),
    "nextmotion_catalog:create": Lot("data"),
    "nextmotion_catalog:reorder": Lot("data"),
    "nextmotion_catalog:add_item": Lot("data"),
    "nextmotion_catalog:assign_distribution": Lot("data"),
    "nextmotion_catalog:update_post_treatment": Lot("data"),
    "origami_rows:upsert": Lot("rows"),
    "outlook:draft": Lot("to", avec=("cc", "bcc")),
    "outlook_act:send": Lot("to", avec=("cc", "bcc")),
    "outlook_calendar_event:create": Lot("attendees"),
    "pennylane_customer:create": Lot("emails"),
    "pennylane_customer:update": Lot("emails"),
    "pennylane_firm_upload_urls": Lot("items"),
    "productlane_docs:move": Lot("article_ids"),
    "productlane_docs:create_group": Lot("article_ids"),
    "productlane_docs:update_group": Lot("article_ids"),
    "productlane_docs:create_draft": Lot("article_ids"),
    "productlane_docs:create": Lot("article_ids"),
    "promptwatch_monitor:create": Lot("initial_prompts"),
    "promptwatch_monitor:update": Lot("initial_prompts"),
    "promptwatch_page_tracker:add": Lot("urls"),
    "promptwatch_prompt:create": Lot("prompt_ids"),
    "promptwatch_prompt:update": Lot("prompt_ids"),
    "promptwatch_prompt:attach_tags": Lot("prompt_ids"),
    "promptwatch_prompt:attach_topics": Lot("prompt_ids"),
    "promptwatch_prompt:activate": Lot("prompt_ids"),
    "promptwatch_shopping:add_tracked": Lot("products"),
    "promptwatch_shopping:update_tracked": Lot("products"),
    "promptwatch_taxonomy:create_tags": Lot("names"),
    "promptwatch_taxonomy:rename_tag": Lot("names"),
    "promptwatch_taxonomy:create_topics": Lot("names"),
    "promptwatch_taxonomy:rename_topic": Lot("names"),
    "resend_contact:add_to_segment": Lot("contacts"),
    "salesforce_record:create": Lot("items"),
    "salesforce_record:update": Lot("items"),
    "salesforce_record:upsert": Lot("items"),
    "signwell_act:send_document": Lot("recipients"),
    "signwell_act:remind_document": Lot("recipients"),
    "signwell_document:create": Lot("recipients"),
    "signwell_document:update_recipients": Lot("recipients"),
    "signwell_document:update_authentication": Lot("recipients"),
    "signwell_template:create": Lot("recipients"),
    "signwell_template:update": Lot("recipients"),
    "signwell_template:create_document": Lot("recipients"),
    "slack_act:create_usergroup": Lot("users"),
    "slack_act:update_usergroup": Lot("users"),
    "slack_act:enable_usergroup": Lot("users"),
    "slack_act:add_usergroup_members": Lot("users"),
    "ubersuggest_keyword_lists:create": Lot("keywords"),
    "ubersuggest_keyword_lists:add": Lot("keywords"),
    "waalaxy_prospect:add": Lot("prospects"),
    "waterfall_company_act:update_rows": Lot("row_ids"),
    "waterfall_email_act:update_rows": Lot("row_ids"),
    "waterfall_people_act:update_rows": Lot("row_ids"),
    "waterfall_person_act:update_rows": Lot("row_ids"),
    "waterfall_phone_act:update_rows": Lot("row_ids"),
    "waterfall_tech_act:update_rows": Lot("row_ids"),
    "webflow_cms:create": Lot("items"),
    "webflow_cms:update": Lot("items"),
    "webflow_publish": Lot("ids"),
    "yousign_creer": Lot("signataires"),
    "zohosign_act:send_from_template": Lot("recipients"),
    "zohosign_act:remind": Lot("recipients"),
    "data_write": Lot("rows"),
    "email_send": Lot("cc"),
    "oto_admin_signal:notify_send": Lot("only"),
    "oto_admin_signal:set_status": Lot("only"),
    "oto_admin_signal:reroute": Lot("only"),
}

#: Tool → its list arguments that are NOT a batch of records (fields, tags, sorts…).
PAS_UN_LOT: dict[str, str] = {
    "affinity_entity": "fields",
    "affinity_list_entry": "fields",
    "affinity_note": "person_ids company_ids opportunity_ids",
    "ahrefs_batch_analysis": "targets select order_by",
    "ahrefs_brand_radar_prompt": "countries",
    "ahrefs_brand_radar_report": "prompts_frequency",
    "ahrefs_project_keywords": "locations tags",
    "airtable_base": "tables",
    "airtable_record": "record_ids select_fields sort merge_on",
    "airtable_table": "fields",
    "allo_act": "tags options sections",
    "amplitude_query": "events segments",
    "apify_google_maps_places": "search_terms fields",
    "apify_google_maps_reviews": "place_urls place_ids fields",
    "apify_indeed_jobs": "fields",
    "apify_linkedin_companies": "urls names fields",
    "apify_linkedin_company_employees": "companies job_titles locations fields",
    "apify_linkedin_jobs": "search_urls company_ids fields",
    "apify_linkedin_posts": "queries author_urls fields",
    "apify_linkedin_profiles": "urls fields",
    "apify_run_sync": "fields",
    "apify_website_contacts": "urls fields",
    "apify_website_crawl": "urls include_globs fields",
    "apify_welcome_to_the_jungle_jobs": "company_slugs contract_types fields",
    "apollo_contact": "contact_stage_ids contact_label_ids label_names",
    "apollo_conversation": "contact_ids tag_ids tracker_ids organization_ids",
    "apollo_email": "attachment_ids stats reply_classes sequence_ids exclude_sequence_ids ids",
    "apollo_sequence": "label_names emailer_steps",
    "apollo_sequence_contacts": "label_names emailer_campaign_ids",
    "attio_entry": "sorts",
    "bls_oews_wages": "areas",
    "brevo_contact": "list_ids unlink_list_ids",
    "brevo_crm_link": "link_contact_ids unlink_contact_ids link_ids unlink_ids",
    "brevo_export_contacts": "export_attributes",
    "brevo_import_contacts": "list_ids",
    "brevo_send_email": "tags",
    "brevoauto_step": "next_steps",
    "claap_recordings": "labels sources",
    "clay_get_run": "fields",
    "clay_search": "fields",
    "clay_search_next": "fields",
    "clay_tables_query": "fields",
    "cognism_enrich_account": "anchor_fields",
    "cognism_enrich_contact": "anchor_fields",
    "cognism_redeem": "ids redeem_ids",
    "dataforseo_ai_visibility": "targets keywords filters order_by",
    "dataforseo_backlinks": "targets exclude_targets filters order_by",
    "dataforseo_domain": "targets keywords filters order_by",
    "dataforseo_keywords": "keywords filters order_by",
    "dataforseo_site_audit": "categories filters order_by",
    "dataforseo_web_mentions": "page_type filters order_by",
    "figma_get_file": "node_ids",
    "figma_get_images": "node_ids",
    "finkare_invoice": "invoices",
    "firecrawl_crawl": "include_paths exclude_paths",
    "firecrawl_extract": "urls",
    "firecrawl_scrape": "formats include_tags exclude_tags actions",
    "firecrawl_search": "sources categories include_domains exclude_domains",
    "fireflies_bite": "privacies",
    "fireflies_live_meeting": "states",
    "fireflies_transcript": "organizers participants",
    "folk_record": "add_to_groups remove_from_groups",
    "folk_webhook": "subscribed_events",
    "foncier_conso_elec": "naf2 code_commune",
    "foncier_icpe": "fields",
    "fullenrich_enrich_linkedin": "enrich_fields",
    "ga4_realtime": "metrics dimensions order_by",
    "ga4_report": "metrics dimensions order_by",
    "github_issues": "labels",
    "gmail": "attachments",
    "gmail_act": "message_ids attachments",
    "granola_webhook_endpoint": "scopes events folder_ids",
    "hellostock_offre_update": "keywords",
    "http_act": "body operations",
    "http_post": "body",
    "hubspot_list": "remove_record_ids properties processing_types",
    "hubspot_object": "properties associations filters sorts merge_ids",
    "hubspot_property": "names",
    "jev_rows": "state_fields filters",
    "kaspr_enrich_linkedin": "data_to_get",
    "klaviyo_consent": "channels",
    "klaviyo_events": "include",
    "klaviyo_profiles": "additional_fields include",
    "leexi_calls": "owner_uuid participating_user_uuid customer_email_address customer_phone_number",
    "lemlist_act": "filters",
    "lemlist_campaign": "campaign_ids sender_user_ids channels",
    "lemlist_campaign_auto_review": "conditions",
    "lemlist_company": "ids_or_domains fields",
    "lemlist_contact": "ids_or_emails",
    "lemlist_deliverability": "scope_entities",
    "lemlist_inbox": "label_ids attachments",
    "lemlist_lead": "variable_names",
    "lemlist_schedule": "weekdays",
    "lemlist_task": "images videos filters",
    "lemlist_watchlist": "filters",
    "lever_add_candidate": "posting_ids",
    "linear_issue": "label_ids include",
    "linear_project": "team_ids include",
    "linear_webhook": "resource_types",
    "luma_act": "recipient_groups",
    "luma_blasts": "recipient_groups",
    "luma_contacts": "tags",
    "luma_guest_admin": "ticket_type_ids remove_ticket_ids",
    "luma_memberships": "registration_answers",
    "luma_webhooks": "event_types",
    "meta_ads_insights": "fields breakdowns action_attribution_windows filtering sort",
    "minari_contact": "contact_ids",
    "modjo_calls": "ids expand participants tags",
    "nextmotion_appointment": "fields",
    "nextmotion_calendar": "fields",
    "nextmotion_catalog": "fields",
    "nextmotion_invoice": "fields",
    "nextmotion_lead": "fields",
    "nextmotion_patient": "fields",
    "nextmotion_payment": "fields",
    "nextmotion_practitioner": "fields",
    "nextmotion_product": "fields",
    "nextmotion_quote": "fields",
    "nextmotion_setting": "label_types fields",
    "notion_append_blocks": "blocks",
    "notion_create_page": "content",
    "notion_edit_markdown": "replacements",
    "notion_view": "sorts clear fields",
    "origami_rows": "match_columns",
    "outlook": "fields attachments",
    "outlook_act": "message_ids attachments",
    "payfit_absence": "status fields",
    "payfit_act": "insurance_contract_ids",
    "payfit_collaborator": "fields",
    "payfit_contract": "fields",
    "payfit_insurance": "insurance_contract_ids fields",
    "pennylane_invoice": "lines categories",
    "pennylane_ledger_entry": "clauses lines categories",
    "pennylane_ledger_lettering": "line_ids",
    "pennylane_quote": "lines",
    "pennylane_supplier_invoice": "invoice_lines categories",
    "productlane_threads": "issue_ids project_ids expand",
    "promptwatch_content": "blocked_words brief_key_points prompt_types intent_types tag_ids topic_ids",
    "promptwatch_content_agent": "statuses publish_windows blackout_dates default_enabled_tools",
    "promptwatch_monitor": "models generate_prompts",
    "promptwatch_prompt": "prompts keywords tags topics types topic_ids",
    "promptwatch_shopping": "models prompt_types intent_types",
    "recruitee_create_candidate": "offer_ids",
    "resend_contact": "segment_ids",
    "sellsy_contact": "fields embed",
    "sellsy_document": "fields embed",
    "sellsy_item": "fields embed",
    "sellsy_opportunity": "fields embed",
    "sellsy_task": "fields embed",
    "sellsy_third_party": "fields embed",
    # a grid of cells written in place, not a batch of records
    "sharepoint_excel_act": "values",
    "sheets_create": "sheets",
    # a grid of cells written in place, not a batch of records
    "sheets_spreadsheet": "options values",
    "signwell_document": "files fields",
    "signwell_template": "template_ids files placeholders template_fields fields",
    "slack_act": "channels",
    "snitcher_custom_field": "field_rules options value",
    "spott_application": "candidate_emails include",
    "spott_candidate": "list_ids include filters",
    "spott_note": "label_ids links",
    "tally_account": "workspace_ids",
    "tally_form": "blocks workspace_ids",
    "tally_webhook": "event_types http_headers",
    "typeform_forms": "operations",
    "typeform_webhooks": "event_types",
    "waterfall_company_act": "providers roles technologies",
    "waterfall_email_act": "providers roles technologies",
    "waterfall_people_act": "providers roles technologies",
    "waterfall_person_act": "providers roles technologies",
    "waterfall_phone_act": "providers roles technologies",
    "waterfall_tech_act": "providers roles technologies",
    "webflow_cms": "ids",
    "webflow_site_publish": "custom_domains",
    "wordpress_article": "categories tags",
    "wttj_candidate": "fields",
    "data_claim_next": "filters",
    "data_review_app": "fields",
    "data_write": "force versions",
    "oto_admin_guide": "slots",
    "oto_apps": "project_ids edits",
    "oto_connector": "connectors",
    "oto_doc": "pages fields",
    "oto_fleet": "tools",
    "oto_function": "requirements",
    "oto_instance": "services",
    "oto_node_edit": "columns",
    "oto_org_settings": "senders rules unmask",
    "oto_procedure": "slots tags",
    "oto_project": "mcp_tools excluded_url_prefixes include fields",
    "oto_resource": "mcp_tools",
    "oto_resource_v2": "mcp_tools",
    "oto_trigger": "trigger_ids tools platform_tools events",
}


# --- reading it -------------------------------------------------------------------------

def classes_par_op(outil: str) -> dict[Optional[str], str]:
    """`{op: class}` of a declared tool (`{None: class}` for a tool without `op`)."""
    if outil not in EFFETS:
        raise EffetNonDeclare(f"`{outil}`: no declared effect")
    decl = EFFETS[outil]
    if isinstance(decl, str):
        return {None: decl}
    return {op: classe for classe, ops in decl.items() for op in ops.split()}


def lot(outil: str, op: Optional[str]) -> Optional[Lot]:
    return LOTS.get(f"{outil}:{op}" if op else outil)


def effet(name: str, args: Optional[dict] = None) -> tuple[str, Optional[Lot]]:
    """`(class, batch)` of a call of `name` with `args`.

    The op is `args["op"]`; a multiplexed tool called WITHOUT `op` takes the most
    constraining class of its ops (its served default is the caller's to resolve, as
    `access_level_gate.default_op` does). A judge (`JUGES`) may set the class from the
    arguments; one in `JUGES_EN_BASE` reads the database (`lit_la_base`: then call this
    off the event loop). A relay (`oto_call`) takes its target's effect (`cible`), and
    raises without one. A tool declared `LECTURE` is not in this table: the caller checks `lecture.en_lecture` on the
    target first. Raises `EffetNonDeclare` for anything undeclared."""
    name, args = cible(name, args)
    par_op = classes_par_op(name)
    if None in par_op:
        op = None
        classe = par_op[None]
    else:
        op = args.get("op")
        if op is None:
            classe = _plus_contraignante(*par_op.values())
        elif op in par_op:
            classe = par_op[op]
        else:
            raise EffetNonDeclare(f"`{name}` op `{op}`: no declared effect")
    juge = JUGES.get(name)
    if juge is not None:
        classe = juge(op, args) or classe
    if classe in ("ecrire", "envoyer"):
        return classe, lot(name, op)
    return classe, None
