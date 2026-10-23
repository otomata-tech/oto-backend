"""The settlement of a call — the single point where a call debits what it consumed on a
shared key (L8, `docs/credits-au-moment-de-l-appel.md`).

A tool no longer debits anything itself. It SAYS what it consumes, on the call record
(`session_org.note_call_trace(units=N)`); the resolver SAYS under which key the call went
(`platform_carrier` for a platform key, `tenant_grant_ids` for a tenant edge); and the
edge that set the call record settles it once the tool has returned. Three edges set a
call record, and each one settles its own:

- `middleware.call_context` — a direct MCP call;
- `tools.meta.executer_cible` — the target of `oto_call` and of the recipes, which has
  its own record (the envelope's never carries these keys, `CLES_REGLEMENT`);
- `jev_jobs_worker` — a background slice.

Rules:

- `units` is the QUOTA axis, not `quantity` — the partner billing axis (provider
  credits, set on the call's own key too). The two differ on purpose on several tools.
- The quota on success only: a call that raised debits no quota.
- The units budget (`access/credits.py`) on success AND on failure: a call that raised
  releases its hold on a net refusal, and settles it at the reserved amount otherwise
  (`regler_echec`).
- A platform resolution without `units` is never settled silently: the tool is either
  declared free here (`SANS_DEBIT`), or the gap is journaled (`units_absentes`) and
  nothing is debited. `tests/test_reglement_appel.py` keeps every tool of a connector
  that can serve a platform key in one of the two.
- Settling never turns a served call into an error: a failed debit is journaled with
  what is needed to correct the count by hand.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import credits, quotas, tenant_budget

logger = logging.getLogger(__name__)

# The keys of the call record that the settlement reads. Never copied from a target's
# record into the envelope's: the target settles them, the envelope would do it twice.
CLES_REGLEMENT = ("units", "platform_carrier", "tenant_grant_ids", credits.CLE)

# Tools of a connector that can serve a PLATFORM key, and that debit NOTHING on it —
# declared, not inferred: a tool missing from here and that notes no `units` is
# journaled at every platform call. Unchanged from before L8: these tools never debited
# the platform quota (a balance read, an account listing, a search the quota does not
# count…). Moving one of them to a debit is a decision, with its `units`.
SANS_DEBIT = frozenset({
    # apollo — only the person match and the bulk match debit the common key's quota.
    "apollo_search_organizations", "apollo_enrich_organization",
    "apollo_bulk_enrich_organizations", "apollo_search_people", "apollo_reveal_phone",
    "apollo_reveal_phone_result", "apollo_job_postings", "apollo_contact",
    "apollo_email_accounts", "apollo_email_schedules", "apollo_sequence",
    "apollo_sequence_contacts", "apollo_email", "apollo_act", "apollo_conversation",
    # fr — only the two INSEE reads (`fr_siret`, `fr_headquarters`) debit the `sirene` key.
    "fr_search", "fr_get", "fr_groupe", "fr_directors", "fr_avis_sirene", "fr_bilans",
    "fr_bilan", "fr_events", "fr_events_batch", "fr_tenders_search", "fr_tenders_get",
    "fr_aides_search", "fr_aides_get", "fr_accords_search", "fr_accords_get",
    "fr_accords_themes", "fr_accords_text", "fr_egapro_declaration",
    "fr_stock_search", "fr_stock_siret", "fr_stock_siege", "fr_stock_etablissements",
    "fr_stock_enrich",
    # fullenrich — the result read is free; the submission debits.
    "fullenrich_result",
    # monid — only a run debits.
    "monid_endpoint", "monid_runs", "monid_wallet",
    # connectors whose platform key never debited the quota.
    "apify_store_search", "apify_actors", "apify_actor", "apify_run_sync", "apify_run",
    "apify_run_status", "apify_abort_run", "apify_dataset_items",
    "dataforseo_keywords", "dataforseo_domain", "dataforseo_backlinks",
    "dataforseo_account", "dataforseo_ai_visibility", "dataforseo_site_audit",
    "dataforseo_technologies", "dataforseo_web_mentions",
    "dropcontact_enrich", "dropcontact_result", "dropcontact_credits",
    "firecrawl_map", "firecrawl_scrape", "firecrawl_search", "firecrawl_crawl",
    "firecrawl_crawl_status", "firecrawl_cancel_crawl", "firecrawl_extract",
    "firecrawl_extract_status",
    "hithorizons_search_company", "hithorizons_search_unstructured",
    "hithorizons_company", "hithorizons_suggestions",
    "jev_ask", "jev_items", "jev_rows", "jev_job",
    "lusha_search_and_enrich",
    # tavily: a daily ceiling of 100 that nothing debits — a known gap, kept as is
    # until the units budget (`docs/credits-au-moment-de-l-appel.md`).
    "tavily_search", "tavily_extract", "tavily_map", "tavily_crawl",
    "topograph_search", "topograph_company",
    "transcription_create", "transcription_status",
    "zerobounce_verify_email", "zerobounce_verify_batch", "zerobounce_credits",
    # unipile — the account and its channels: a paid option and operated accounts
    # govern its platform mode, not a debited quota.
    "unipile_connect_start",
    "linkedin_unipile", "linkedin_unipile_act", "linkedin_unipile_account",
    "linkedin_unipile_facets", "linkedin_unipile_job",
    "linkedin_unipile_network", "linkedin_unipile_post", "linkedin_unipile_profile",
    "linkedin_unipile_search",
    "whatsapp", "whatsapp_act", "telegram", "telegram_act", "instagram", "instagram_act",
})


def regler_appel(sub: Optional[str], outil: str, releve: Optional[dict]) -> None:
    """Settles the call `outil` that SUCCEEDED, from its call record `releve`. Called by
    the three edges only, off the event loop (it writes), after the tool returned.

    Never raises: the call was served, a counting failure must not undo that."""
    if not releve:
        return
    _debiter_le_quota(sub, outil, releve)
    _regler_le_budget(outil, releve, credits.solder_reussite, releve)


def regler_echec(sub: Optional[str], outil: str, releve: Optional[dict],
                 exc: BaseException) -> None:
    """Settles the call `outil` that RAISED `exc`: its units budget only — the quota
    counts successes. Called by the three edges only, off the event loop.

    Never raises: the edge re-raises the call's own error."""
    if not releve:
        return
    _regler_le_budget(outil, releve, credits.solder_echec, releve, exc)


def _debiter_le_quota(sub: Optional[str], outil: str, releve: dict) -> None:
    try:
        aretes = releve.get("tenant_grant_ids")
        if aretes:
            tenant_budget.regler_aretes_tenant(aretes)
        porteur = releve.get("platform_carrier")
        if porteur is None:
            return
        unites = releve.get("units")
        if unites is None:
            if outil not in SANS_DEBIT:
                logger.warning(
                    "units_absentes : l'outil %s a servi la clé plateforme %s sans dire "
                    "ce qu'il consomme — rien n'est débité (sub=%s). Poser `units` dans "
                    "l'outil, ou le déclarer dans `reglement.SANS_DEBIT`.",
                    outil, porteur, sub)
            return
        if unites > 0 and sub:
            quotas.debiter_plateforme(sub, porteur, int(unites))
    except Exception:  # noqa: BLE001 — the call was served; the count is what is lost
        logger.warning(
            "règlement ÉCHOUÉ — outil=%s sub=%s porteur=%s units=%s arêtes tenant=%s "
            "(l'appel a réussi, le compteur n'a PAS bougé)",
            outil, sub, releve.get("platform_carrier"), releve.get("units"),
            releve.get("tenant_grant_ids"), exc_info=True)


def _regler_le_budget(outil: str, releve: dict, regle, *args) -> None:
    try:
        regle(*args)
    except Exception:  # noqa: BLE001 — the hold stays open; its deadline settles it
        appel = releve.get(credits.CLE) or {}
        logger.warning(
            "règlement du budget ÉCHOUÉ — outil=%s réservation=%s (elle reste ouverte : "
            "son échéance la règle au montant réservé)", outil, appel.get("uid"),
            exc_info=True)
