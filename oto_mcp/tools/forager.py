"""Forager.ai — job posts, organization firmographics, people/contact enrichment.

**Live-tested 2026-08-21** against a real Trial-tier key, one call per op
(36.00/50.00 credits spent) — every tool below confirmed working end to end.
Two behaviors confirmed live and worth knowing before calling `forager_person`:
`op="work_emails"`/`"personal_emails"`/`"phone_numbers"` return `[]` on no
match (not billed), while `op="reverse_by_email"`/`"reverse_by_phone"` raise
a tool error (404 from Forager) on no match instead — check for the right
kind of "nothing found" per op. Also: `forager_autocomplete`'s `id` values
are strings; cast to `int` before using them in a search `filters` dict.

Credential = `X-API-KEY` header + an optional `account_id` (generic
multi-field model, ADR 0011 — resolved via
`access.resolve_credential_fields("forager")`, not `resolve_api_key`).
`account_id` scopes every datastorage/subscriptions call and is normally
resolved automatically from `GET /api/users/current/` (`ForagerClient`,
oto-core) — it only needs to be entered explicitly if the key has access to
MORE than one Forager account, in which case the client REFUSES rather than
guessing which one to bill (`ValueError`, converted below to `McpError`
`INVALID_PARAMS` naming the `account_id` credential field — an unhandled
`ValueError` would otherwise surface to the agent as an opaque internal
error and land in Sentry as if it were a backend defect).

**Paid, per-lookup API.** Every search/lookup op below bills a credit against
the account's subscription except `forager_account` and
`forager_autocomplete`, which are metadata/free. Use `op="totals"` (job post,
organization, person role search) to get a count without paying for the full
result page.

**IDs, not free text.** `locations`, `industries`, `industries_exclude`,
`keywords`/`organization_keywords`, `web_technologies`/
`organization_web_technologies` and `person_skills` filters all take integer
IDs — resolve free text to an ID via `forager_autocomplete` FIRST, passing a
raw string directly into these filters will not match.

**No API-key management here, deliberately.** Forager's API exposes
`GET/POST /api/api_keys/` and `GET/DELETE /api/api_keys/{prefix}/`, wrapped
by neither `ForagerClient` nor this module: creating a key is a
secret-issuing operation (the value would have to transit LLM context to be
useful) and deleting one can silently break another integration — both are
dashboard-only by this codebase's convention. Manage keys at
https://app.forager.ai.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /api/users/current/` (déjà dans le client — `get_current_user`),
    documenté « Free » dans le client ET dans `forager_account` (op="me") —
    le seul appel confirmé sans coût de ce connecteur payant au crédit.

    Pas `auth+quota` : `credits_balance` vit PAR COMPTE dans `accounts[]`, et
    une clé avec accès à plusieurs comptes n'en désigne aucun par défaut
    (`resolve_account_id` REFUSE plutôt que deviner, cf. docstring du module)
    — lire un solde ici demanderait de choisir un compte au hasard.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope —
    rien de plus fin que l'identité à distinguer sur cet appel.
    """
    from oto.tools.forager import ForagerClient

    ForagerClient(api_key=fields["api_key"]).get_current_user()


def register(mcp: FastMCP) -> None:
    from oto.tools.forager import ForagerClient

    connector_verify.register("forager", _verify)

    def _client() -> ForagerClient:
        creds = access.resolve_credential_fields("forager")
        return ForagerClient(api_key=creds.get("api_key"), account_id=creds.get("account_id"))

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _present(value) -> bool:
        """The one definition of 'was this argument actually given' — `None`
        and `""` both count as absent, everywhere (exclusivity checks AND
        refusal checks). A single predicate so the two can't drift apart."""
        return value is not None and value != ""

    def _need(value, name: str, op: str):
        if not _present(value):
            raise _bad(f"op='{op}' requiert {name}")
        return value

    def _refuse_ignored(op: str, hint: str, **provided) -> None:
        """An argument this op doesn't use is a mistaken-intent signal, not a
        detail to drop silently — see silae.py for the incident this pattern
        guards against (a filter that looked honored but was quietly ignored)."""
        for name, value in provided.items():
            if _present(value):
                raise _bad(f"op='{op}' n'utilise pas {name} — {hint}")

    def _refuse_filters(op: str, filters: Optional[dict]) -> None:
        if filters:
            raise _bad(f"op='{op}' n'utilise pas filters — réservé aux ops de recherche")

    # --- Job posts ---

    @mcp.tool()
    def forager_job_post(
        op: Literal["search", "totals"] = "search",
        filters: Optional[dict] = None,
    ) -> object:
        """Search Forager's job-posting events, or just their count.

        `op="search"` bills a credit; `op="totals"` first is the cheap way to
        check volume before paying for the full result page.

        `filters` (all optional):
        - page (int)
        - job_source: 'indeed' | 'linkedin' | 'angellist'
        - date_featured_start / date_featured_end (ISO date)
        - organization_ids ([int])
        - title (str), description (str) — free text
        - is_remote (bool), is_active (bool)
        - locations / locations_exclude ([int] IDs — resolve via
          `forager_autocomplete(op="locations", q=...)`)
        """
        client = _client()
        f = filters or {}
        try:
            if op == "search":
                return client.search_job_posts(**f)
            if op == "totals":
                return client.search_job_posts_totals(**f)
        except ValueError as e:
            raise _bad(str(e))
        raise _bad("op doit être 'search' ou 'totals'")

    # --- Organizations (+ website lookup, folded in) ---

    @mcp.tool()
    def forager_organization(
        op: Literal["search", "totals", "website"] = "search",
        filters: Optional[dict] = None,
        domain: Optional[str] = None,
        organization_id: Optional[int] = None,
        organization_linkedin_public_identifier: Optional[str] = None,
    ) -> object:
        """Search organizations by firmographics, or look up one website's tech stack.

        `op="search"`/`"totals"`: bills a credit ("search") — `filters` (all
        optional): page, organization_ids ([int]), description (str),
        locations/industries/industries_exclude/web_technologies ([int] IDs —
        via `forager_autocomplete`), keywords ([int] — via
        `forager_autocomplete(op="organization_keywords", ...)`),
        employees_start/end, founded_date_start/end, revenue_start/end,
        domains ([str]), domain_rank_start/end, domain_traffic_start/end,
        linkedin_public_identifiers ([str]), funding_types ([str enum]),
        funding_total_start/end, funding_event_date_featured_start/end,
        job_post_title/description/is_remote/is_active/date_featured_start/
        date_featured_end/locations/locations_exclude (nested job-post
        filters, same shape as `forager_job_post`), simple_event_source/
        reason/date_featured_start/end.

        `op="website"`: bills a credit — pass exactly ONE of `domain`,
        `organization_id`, `organization_linkedin_public_identifier`.
        """
        client = _client()
        try:
            if op in ("search", "totals"):
                _refuse_ignored(
                    op, "utilise op='website' pour un lookup de site",
                    domain=domain, organization_id=organization_id,
                    organization_linkedin_public_identifier=organization_linkedin_public_identifier,
                )
                f = filters or {}
                return client.search_organizations(**f) if op == "search" else client.search_organizations_totals(**f)
            if op == "website":
                _refuse_filters(op, filters)
                given = [x for x in (domain, organization_id, organization_linkedin_public_identifier) if _present(x)]
                if len(given) != 1:
                    raise _bad(
                        "op='website' requiert exactement un de domain / organization_id / "
                        "organization_linkedin_public_identifier"
                    )
                return client.lookup_website(
                    domain=domain, organization_id=organization_id,
                    organization_linkedin_public_identifier=organization_linkedin_public_identifier,
                )
        except ValueError as e:
            raise _bad(str(e))
        raise _bad("op doit être 'search', 'totals' ou 'website'")

    # --- People ---

    @mcp.tool()
    def forager_person(
        op: Literal[
            "detail", "reverse_by_email", "reverse_by_phone",
            "work_emails", "personal_emails", "phone_numbers",
            "role_search", "role_search_totals",
        ] = "detail",
        person_id: Optional[int] = None,
        linkedin_public_identifier: Optional[str] = None,
        email: Optional[str] = None,
        phone_number: Optional[str] = None,
        do_contacts_enrichment: Optional[bool] = None,
        filters: Optional[dict] = None,
    ) -> object:
        """A person's profile, contact details, or a role-based search across people.

        Each op below bills a credit.

        `op="detail"`/`"work_emails"`/`"personal_emails"`/`"phone_numbers"`:
        pass exactly ONE of `person_id` / `linkedin_public_identifier`.
        `personal_emails` only returns free-webmail addresses (Gmail,
        Outlook, Yahoo, iCloud, Proton…), never corporate/edu domains.
        `do_contacts_enrichment` only applies to `"work_emails"`.

        `op="reverse_by_email"`: requires `email`. Raises on no match
        (confirmed live: Forager 404s) — unlike the `[]`-on-no-match ops above.
        `op="reverse_by_phone"`: requires `phone_number`. Same no-match
        behavior as `reverse_by_email`.

        `op="role_search"`/`"role_search_totals"`: `filters` (all optional) —
        page, role_title/description (str), role_is_current (bool),
        role_position_start_date/end_date, role_years_on_position_start/end,
        person_name/headline/description (str), person_skills/locations/
        industries/industries_exclude ([int] IDs — via
        `forager_autocomplete`), person_linkedin_public_identifiers ([str]),
        organizations ([int]), organizations_bulk_domain (str),
        organization_domains ([str]), organization_description (str),
        organization_locations/industries/industries_exclude/keywords/
        web_technologies ([int] IDs), organization_founded_date_start/end,
        organization_employees_start/end, organization_revenue_start/end,
        organization_domain_rank_start/end,
        organization_linkedin_public_identifiers ([str]), funding_types
        ([str enum]), funding_total_start/end,
        funding_event_date_featured_start/end, job_post_* (same shape as
        `forager_job_post` filters), simple_event_source/reason/
        date_featured_start/end. Unlike the other totals endpoints,
        `role_search_totals` also breaks down `total_persons`/
        `total_organizations` alongside `total_search_results`.
        """
        client = _client()

        try:
            if op in ("detail", "work_emails", "personal_emails", "phone_numbers"):
                _refuse_filters(op, filters)
                _refuse_ignored(
                    op, "utilise op='reverse_by_email'/'reverse_by_phone' pour ça",
                    email=email, phone_number=phone_number,
                )
                if op != "work_emails":
                    _refuse_ignored(
                        op, "do_contacts_enrichment ne s'applique qu'à op='work_emails'",
                        do_contacts_enrichment=do_contacts_enrichment,
                    )
                given = [x for x in (person_id, linkedin_public_identifier) if _present(x)]
                if len(given) != 1:
                    raise _bad(f"op='{op}' requiert exactement un de person_id / linkedin_public_identifier")
                if op == "detail":
                    return client.lookup_person_detail(person_id=person_id, linkedin_public_identifier=linkedin_public_identifier)
                if op == "work_emails":
                    return client.lookup_person_work_emails(
                        person_id=person_id, linkedin_public_identifier=linkedin_public_identifier,
                        do_contacts_enrichment=do_contacts_enrichment,
                    )
                if op == "personal_emails":
                    return client.lookup_person_personal_emails(person_id=person_id, linkedin_public_identifier=linkedin_public_identifier)
                return client.lookup_person_phone_numbers(person_id=person_id, linkedin_public_identifier=linkedin_public_identifier)

            if op == "reverse_by_email":
                _refuse_filters(op, filters)
                _refuse_ignored(
                    op, "utilise op='detail' pour un lookup direct",
                    person_id=person_id, linkedin_public_identifier=linkedin_public_identifier,
                    phone_number=phone_number, do_contacts_enrichment=do_contacts_enrichment,
                )
                return client.lookup_person_by_email(_need(email, "email", op))

            if op == "reverse_by_phone":
                _refuse_filters(op, filters)
                _refuse_ignored(
                    op, "utilise op='detail' pour un lookup direct",
                    person_id=person_id, linkedin_public_identifier=linkedin_public_identifier,
                    email=email, do_contacts_enrichment=do_contacts_enrichment,
                )
                return client.lookup_person_by_phone_number(_need(phone_number, "phone_number", op))

            if op in ("role_search", "role_search_totals"):
                _refuse_ignored(
                    op, "sélectionne par filtres — utilise op='detail' pour un person_id/linkedin_public_identifier précis",
                    person_id=person_id, linkedin_public_identifier=linkedin_public_identifier,
                    email=email, phone_number=phone_number, do_contacts_enrichment=do_contacts_enrichment,
                )
                f = filters or {}
                return client.search_person_roles(**f) if op == "role_search" else client.search_person_roles_totals(**f)
        except ValueError as e:
            raise _bad(str(e))

        raise _bad(
            "op doit être 'detail', 'reverse_by_email', 'reverse_by_phone', 'work_emails', "
            "'personal_emails', 'phone_numbers', 'role_search' ou 'role_search_totals'"
        )

    # --- Feedback (report a lookup's contact as correct/incorrect) ---

    @mcp.tool()
    def forager_feedback(
        op: Literal["personal_email", "phone_number", "work_email"],
        contact_status: str,
        is_correct_person: bool,
        email: Optional[str] = None,
        phone_number: Optional[str] = None,
        name: Optional[str] = None,
        person_id: Optional[int] = None,
    ) -> object:
        """Report whether a contact returned by a Forager person lookup was correct.

        ⚠️ Every call creates a NEW record (non-idempotent, `201 Created`) —
        never a dedup/upsert. Do not call twice for the same fact.

        `op="personal_email"`/`"work_email"`: `contact_status` = 'valid' |
        'invalid'. Requires `email`.
        `op="phone_number"`: `contact_status` = 'connected' | 'disconnected'
        (the live-call outcome). Requires `phone_number`.
        `name`/`person_id` are optional context, not required for either.
        """
        client = _client()
        try:
            if op in ("personal_email", "work_email"):
                _refuse_ignored(op, "utilise op='phone_number' pour un numéro de téléphone", phone_number=phone_number)
                email = _need(email, "email", op)
                if op == "personal_email":
                    return client.submit_personal_email_feedback(email, contact_status, is_correct_person, name=name, person_id=person_id)
                return client.submit_work_email_feedback(email, contact_status, is_correct_person, name=name, person_id=person_id)
            if op == "phone_number":
                _refuse_ignored(op, "utilise op='personal_email'/'work_email' pour un email", email=email)
                return client.submit_phone_number_feedback(
                    _need(phone_number, "phone_number", op), contact_status, is_correct_person,
                    name=name, person_id=person_id,
                )
        except ValueError as e:
            raise _bad(str(e))
        raise _bad("op doit être 'personal_email', 'phone_number' ou 'work_email'")

    # --- Autocomplete (free-text → integer ID resolution) ---

    @mcp.tool()
    def forager_autocomplete(
        op: Literal["industries", "organizations", "organization_keywords", "locations", "person_skills", "web_technologies"],
        q: str,
        page: Optional[int] = None,
    ) -> object:
        """Resolve free text to the integer IDs required by ID-typed filters
        elsewhere in this connector (`locations`, `industries`,
        `industries_exclude`, `keywords`/`organization_keywords`,
        `web_technologies`/`organization_web_technologies`, `person_skills`
        on `forager_job_post`/`forager_organization`/`forager_person`).
        Passing raw text directly into those filters will not match — look
        the ID up here first. Free, no credit cost. ⚠️ Returns `results[].id`
        as a STRING (confirmed live) — cast to `int` before putting it in a
        `filters` dict, whose fields are typed as integer arrays.
        """
        client = _client()
        try:
            return getattr(client, f"autocomplete_{op}")(q, page=page)
        except ValueError as e:
            raise _bad(str(e))

    # --- Account (identity, credit balance, usage — read-only) ---

    @mcp.tool()
    def forager_account(
        op: Literal["me", "balance_log", "balance_totals"] = "me",
        date_created_start: Optional[str] = None,
        date_created_end: Optional[str] = None,
        page: Optional[int] = None,
    ) -> object:
        """Account identity, subscription/credit balance, and credit-spend history.

        Free — no credit cost. Does NOT expose API-key management
        (create/list/delete): that stays dashboard-only at
        https://app.forager.ai, deliberately never agent-callable.

        `op="me"` (default): identity + per-account subscription tier +
        `credits_balance`.
        `op="balance_log"`: paginated log of credit balance changes
        (`date_created_start`/`date_created_end`/`page` all optional).
        `op="balance_totals"`: total credits spent in
        `[date_created_start, date_created_end]` — no `page`.
        """
        client = _client()
        try:
            if op == "me":
                _refuse_ignored(
                    op, "op='me' n'a pas de plage de dates",
                    date_created_start=date_created_start, date_created_end=date_created_end, page=page,
                )
                return client.get_current_user()
            if op == "balance_log":
                return client.list_balance_change_logs(
                    date_created_start=date_created_start, date_created_end=date_created_end, page=page,
                )
            if op == "balance_totals":
                _refuse_ignored(op, "page ne s'applique pas à balance_totals", page=page)
                return client.get_balance_change_totals(date_created_start=date_created_start, date_created_end=date_created_end)
        except ValueError as e:
            raise _bad(str(e))
        raise _bad("op doit être 'me', 'balance_log' ou 'balance_totals'")
