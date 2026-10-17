"""Amplemarket tools — B2B prospecting and outbound.

Wraps `oto.tools.amplemarket.AmplemarketClient` (REST, `Authorization: Bearer`).
The connector serves its PAIR (`tests/test_convention_act.py`):

- `amplemarket` (LECTURE) — the workspace, Prospect Hub accounts and contacts,
  the people/company searcher, single enrichment, batch results, job openings,
  lead lists, sequences, tasks, calls, mailboxes and exclusion lists;
- `amplemarket_act` (ACTE) — everything that changes Amplemarket: batch
  enrichments and email validations (start, cancel), lead lists, sequence
  enrollment, contacts, tasks, logged calls, mailbox limits, exclusion lists,
  phone-number reviews.

⚠️ Searches and single enrichments read, but bill the account's credits when they
reveal an email or a phone (402 `insufficient_credits` once spent).

Client calls are written out (`client.find_person(…)`, the client bound by
`client = _client()`) so the version-skew probe (`test_tools_client_methods_exist`)
can check them.
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

_READ_OPS = (
    "account_info", "users", "personas", "territories", "accounts", "account",
    "mailboxes", "search_people", "search_companies", "find_person", "find_company",
    "person_enrichment", "company_enrichment", "email_validation", "job_openings",
    "job_opening", "lead_lists", "lead_list", "sequences", "contacts", "contact",
    "tasks", "task_statuses", "task_types", "calls", "call_dispositions",
    "excluded_emails", "excluded_domains",
)

_ACT_OPS = (
    "create_person_enrichment", "cancel_person_enrichment",
    "create_company_enrichment", "cancel_company_enrichment",
    "create_email_validation", "cancel_email_validation",
    "create_lead_list", "add_lead_list_leads", "add_sequence_leads",
    "create_contact", "set_task_completed", "set_task_skipped", "create_call_log",
    "set_mailbox_daily_limit", "add_excluded_emails", "add_excluded_domains",
    "remove_excluded_emails", "remove_excluded_domains", "review_phone_number",
)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value: Any, name: str, op: str) -> Any:
    if value is None or value == "" or value == []:
        raise _bad(f"`{name}` is required for op='{op}'. Nothing was done.")
    return value


def _upstream_message(e) -> str:
    """Turn an Amplemarket refusal into an actionable message, by status."""
    status = e.status_code
    detail = e.body if isinstance(e.body, (str, dict)) else ""
    if status == 401:
        return ("Amplemarket rejected the key (401) — check the API key set on this "
                "connector (an admin creates it under Settings > API).")
    if status == 402:
        return ("Amplemarket: no credits left (402 insufficient_credits) — the reveal, "
                "enrichment or email validation was not done.")
    if status == 403:
        return f"Amplemarket refused the operation (403): {detail}"
    if status == 404:
        return f"Amplemarket: not found (404) — {detail}."
    if status == 409:
        return f"Amplemarket: conflict (409), it already exists — {detail}."
    if status == 410:
        return f"Amplemarket: gone (410) — the object was deleted or has closed. {detail}"
    if status == 422:
        return f"Amplemarket could not process the leads (422): {detail}"
    if status == 429:
        return "Amplemarket: too many requests (429) — retry later."
    if status == 451:
        return ("Amplemarket found this person but removed their data for privacy "
                "reasons (451).")
    if status in (500, 502, 503, 504):
        return f"Amplemarket is temporarily unavailable (HTTP {status}) — retry later."
    return f"Amplemarket refused the request (HTTP {status}): {detail}"


#: What the default (non-`full`) view drops from each searcher row: long prose and
#: nested histories nobody reads at triage. `full=True` returns the raw rows.
_PERSON_ROW_DROP = ("about", "current_position_description", "experiences", "educations",
                    "languages", "image_url", "location_details", "linkedin_presence")
_COMPANY_ROW_DROP = ("overview", "keywords", "locations", "location_details", "logo_url",
                     "technologies", "department_headcount", "job_function_headcount",
                     "sic_codes", "naics_codes", "linkedin_presence")
_COMPANY_BRIEF = ("id", "name", "website", "linkedin_url", "size", "location")


def _slim(op: str, result: Any) -> Any:
    """The default view: searcher rows without their long prose and histories (a
    person's `company` cut to its identity), the HAL `_links` replaced by
    `next_page_after`. Tolerant of any shape."""
    if not isinstance(result, dict):
        return result
    out = output_projection.project(result, drop=("_links",))
    if op == "search_people":
        out = output_projection.project(out, items_path="results",
                                        item_drop=_PERSON_ROW_DROP)
        if isinstance(out.get("results"), list):
            out["results"] = [
                {**row, "company": {k: v for k, v in row["company"].items()
                                    if k in _COMPANY_BRIEF}}
                if isinstance(row, dict) and isinstance(row.get("company"), dict) else row
                for row in out["results"]]
    elif op == "search_companies":
        out = output_projection.project(out, items_path="results",
                                        item_drop=_COMPANY_ROW_DROP)
    return out


def _with_cursor(result: Any) -> Any:
    """Surface the next-page cursor of a cursor-paged list as `next_page_after`."""
    from oto.tools.amplemarket import next_page_after
    if isinstance(result, dict) and "_links" in result:
        return {**result, "next_page_after": next_page_after(result)}
    return result


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: reads the workspace (`get_account_info`) — no side
    effect, a clean 401 if the key is wrong."""
    from oto.tools.amplemarket import AmplemarketClient
    AmplemarketClient(api_key=fields["key"]).get_account_info()


def register(mcp: FastMCP) -> None:
    from oto.tools.amplemarket import AmplemarketClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register("amplemarket", _verify)

    def _client() -> AmplemarketClient:
        key, _ = access.resolve_api_key("amplemarket")
        return AmplemarketClient(api_key=key)

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    @mcp.tool(name="amplemarket", annotations=LECTURE)
    def amplemarket(
        op: Literal[_READ_OPS],
        id: Optional[str] = None,
        email: Optional[str] = None,
        name: Optional[str] = None,
        domain: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        company_name: Optional[str] = None,
        company_domain: Optional[str] = None,
        title: Optional[str] = None,
        reveal_email: Optional[bool] = None,
        reveal_phone_numbers: Optional[bool] = None,
        filters: Optional[dict] = None,
        ids: Optional[List[str]] = None,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
        type: Optional[str] = None,
        owner_email: Optional[str] = None,
        tags: Optional[List[str]] = None,
        enabled: Optional[bool] = None,
        direction: Optional[str] = None,
        start_date_from: Optional[str] = None,
        start_date_to: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        page_after: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """Amplemarket — read the workspace, search and enrich prospects.

        Cursor-paged lists answer `next_page_after`: pass it back as `page_after`
        for the next page (absent = last page). The searcher pages by number
        (`page` from 1, `page_size`) and answers `_pagination`. Searcher rows come
        without long prose and histories (about, experiences, company overview…):
        `full=True` returns them raw.

        Ops:
        - `account_info` — the workspace. `users` (`email`, `status`) — user ids
          for `tasks` and the act tool. `personas` (`name`, `enabled`),
          `territories` (`name`). `mailboxes` (`status`: active / inactive /
          needs_reconnection, `email`=user email).
        - `accounts` (`name`, `domain`, `owner_email`, `tags`) / `account` (`id`) —
          Prospect Hub companies.
        - `search_people` / `search_companies` — `filters` dict, every key
          optional, ANDed: person_titles, person_seniorities (Owner, Founder,
          C-Suite, Partner, VP, Head, Director, Manager, Senior, Entry, Intern,
          Other), person_departments, person_job_functions, person_locations,
          person_keywords, company_names, company_domains, company_locations,
          company_sizes ("51-200 employees"…), company_industries,
          company_keywords, company_revenue, company_last_funding, job_openings,
          headcount_growth, … (exclude_ variants too). No email is revealed.
        - `find_person` — ONE person by `email`, `linkedin_url`, or `name` +
          `company_name`/`company_domain` (`title` helps); `reveal_email` /
          `reveal_phone_numbers` spend credits. `find_company` — by `domain` or
          `linkedin_url`.
        - `person_enrichment` / `company_enrichment` / `email_validation` (`id`) —
          status and results of a batch started with `amplemarket_act`.
        - `job_openings` (`domain`, `linkedin_url` or `id`=company id) /
          `job_opening` (`id`).
        - `lead_lists` (`status`, `owner_email`) / `lead_list` (`id`, with a page
          of its leads). `sequences` (`status`, `name`).
        - `contacts` (`ids` ≤ 20, or `name` / `account_id`) / `contact` (`id` or
          `email`).
        - `tasks` (`user_id` required, `status`, `type`) / `task_statuses` /
          `task_types`. `calls` (`user_id`, `direction`, `start_date_from`,
          `start_date_to` ISO 8601) / `call_dispositions`.
        - `excluded_emails` (`email` keyword) / `excluded_domains` (`domain`
          keyword).
        """
        cursor = dict(page_size=page_size, page_after=page_after)

        def call():
            client = _client()
            if op == "account_info":
                return client.get_account_info()
            if op == "users":
                return _with_cursor(client.list_users(status=status, email=email, **cursor))
            if op == "personas":
                return _with_cursor(client.list_personas(name=name, enabled=enabled, **cursor))
            if op == "territories":
                return _with_cursor(client.list_territories(name=name, **cursor))
            if op == "accounts":
                return _with_cursor(client.list_accounts(
                    name=name, domain=domain, owner_email=owner_email, tags=tags, **cursor))
            if op == "account":
                return client.get_account(_need(id, "id", op))
            if op == "mailboxes":
                return _with_cursor(client.list_mailboxes(
                    status=status, user_email=email, **cursor))
            if op == "search_people":
                return client.search_people(page=page, page_size=page_size, **(filters or {}))
            if op == "search_companies":
                return client.search_companies(page=page, page_size=page_size,
                                               **(filters or {}))
            if op == "find_person":
                return client.find_person(
                    email=email, linkedin_url=linkedin_url, name=name, title=title,
                    company_name=company_name, company_domain=company_domain,
                    reveal_email=reveal_email, reveal_phone_numbers=reveal_phone_numbers)
            if op == "find_company":
                return client.find_company(domain=domain, linkedin_url=linkedin_url)
            if op == "person_enrichment":
                return client.get_person_enrichment(_need(id, "id", op))
            if op == "company_enrichment":
                return client.get_company_enrichment(_need(id, "id", op))
            if op == "email_validation":
                return _with_cursor(client.get_email_validation(_need(id, "id", op), **cursor))
            if op == "job_openings":
                return _with_cursor(client.list_job_openings(
                    company_id=id, domain=domain, linkedin_url=linkedin_url, **cursor))
            if op == "job_opening":
                return client.get_job_opening(_need(id, "id", op))
            if op == "lead_lists":
                return _with_cursor(client.list_lead_lists(
                    status=status, owner_email=owner_email, **cursor))
            if op == "lead_list":
                return _with_cursor(client.get_lead_list(_need(id, "id", op), **cursor))
            if op == "sequences":
                return _with_cursor(client.list_sequences(status=status, name=name, **cursor))
            if op == "contacts":
                return client.list_contacts(ids=ids, name=name, account_id=account_id)
            if op == "contact":
                if id:
                    return client.get_contact(id)
                return client.get_contact_by_email(_need(email, "id` or `email", op))
            if op == "tasks":
                return _with_cursor(client.list_tasks(
                    _need(user_id, "user_id", op), status=status, type=type, **cursor))
            if op == "task_statuses":
                return client.list_task_statuses()
            if op == "task_types":
                return client.list_task_types()
            if op == "calls":
                return _with_cursor(client.list_calls(
                    user_id=user_id, direction=direction, start_date_from=start_date_from,
                    start_date_to=start_date_to, **cursor))
            if op == "call_dispositions":
                return client.list_call_dispositions()
            if op == "excluded_emails":
                return _with_cursor(client.list_excluded_emails(email=email, **cursor))
            if op == "excluded_domains":
                return _with_cursor(client.list_excluded_domains(domain=domain, **cursor))
            raise _bad(f"unknown op '{op}' — one of: {', '.join(_READ_OPS)}")

        result = _run(call)
        return result if full else _slim(op, result)

    @mcp.tool(name="amplemarket_act", annotations=ACTE)
    def amplemarket_act(
        op: Literal[_ACT_OPS],
        id: Optional[str] = None,
        leads: Optional[List[dict]] = None,
        companies: Optional[List[dict]] = None,
        emails: Optional[List[str]] = None,
        domains: Optional[List[str]] = None,
        reveal_email: Optional[bool] = None,
        reveal_phone_numbers: Optional[bool] = None,
        name: Optional[str] = None,
        owner: Optional[str] = None,
        type: Optional[str] = None,
        shared: bool = False,
        options: Optional[dict] = None,
        settings: Optional[dict] = None,
        contact: Optional[dict] = None,
        call: Optional[dict] = None,
        user_id: Optional[str] = None,
        set_lead_to_completed: Optional[bool] = None,
        daily_email_limit: Optional[int] = None,
    ) -> Any:
        """Amplemarket — change the workspace: batches, lead lists, sequences, contacts, tasks.

        Ops:
        - `create_person_enrichment` — `leads` (≤ 10,000, each `{email |
          linkedin_url | name + company_name/company_domain, title?}`),
          `reveal_email`, `reveal_phone_numbers` (spend credits). Answers the
          batch `id`: read it with `amplemarket(op="person_enrichment", id=…)`.
          `create_company_enrichment` — `companies` (`{domain | linkedin_url}`).
          `create_email_validation` — `emails` (≤ 100,000; spends credits).
          `cancel_person_enrichment` / `cancel_company_enrichment` /
          `cancel_email_validation` — `id`.
        - `create_lead_list` — `owner` (user email), `type` (linkedin / email /
          titles_and_company), `leads` (≤ 10,000: linkedin_url, email, title,
          company_name, company_domain), `name`, `shared`, `options`
          (reveal_phone_numbers, validate_email, enrich — spend credits).
          `add_lead_list_leads` — `id` (lead list) + `leads`, optional `type`.
        - `add_sequence_leads` — ⚠️ `id` (sequence) + `leads` (`{email |
          linkedin_url, data?: {variable: value}, overrides?}`), optional
          `settings` (`leads_distribution`, `mailboxes`): an ACTIVE sequence
          starts emailing or messaging them. Answers counts added / skipped.
        - `create_contact` — `contact` dict: email (required), first_name,
          last_name, title, linkedin_url, owner_email, company_domain, …
        - `set_task_completed` — `id` (task), `set_lead_to_completed`.
          `set_task_skipped` — `id`.
        - `create_call_log` — `call` dict: task_id, user_id, from_number,
          to_number, duration (s), answered, human, disposition_id?,
          transcription?, recording_url?.
        - `set_mailbox_daily_limit` — `id` (mailbox) + `daily_email_limit`
          (one change per mailbox per hour).
        - `add_excluded_emails` / `remove_excluded_emails` — `emails`;
          `add_excluded_domains` / `remove_excluded_domains` — `domains`.
          ⚠️ removing an exclusion lets sequences contact them again.
        - `review_phone_number` — `id` (phone number) + `user_id`: flags it wrong.
        """
        def act():
            client = _client()
            if op == "create_person_enrichment":
                return client.start_person_enrichment(
                    _need(leads, "leads", op), reveal_email=reveal_email,
                    reveal_phone_numbers=reveal_phone_numbers)
            if op == "cancel_person_enrichment":
                return client.cancel_person_enrichment(_need(id, "id", op))
            if op == "create_company_enrichment":
                return client.start_company_enrichment(_need(companies, "companies", op))
            if op == "cancel_company_enrichment":
                return client.cancel_company_enrichment(_need(id, "id", op))
            if op == "create_email_validation":
                return client.start_email_validation(_need(emails, "emails", op))
            if op == "cancel_email_validation":
                return client.cancel_email_validation(_need(id, "id", op))
            if op == "create_lead_list":
                return client.create_lead_list(
                    _need(owner, "owner", op), _need(type, "type", op),
                    _need(leads, "leads", op), shared=shared, name=name, options=options)
            if op == "add_lead_list_leads":
                return client.add_leads_to_lead_list(
                    _need(id, "id", op), _need(leads, "leads", op), type=type)
            if op == "add_sequence_leads":
                return client.add_leads_to_sequence(
                    _need(id, "id", op), _need(leads, "leads", op), settings=settings)
            if op == "create_contact":
                return client.create_contact(**_need(contact, "contact", op))
            if op == "set_task_completed":
                return client.complete_task(
                    _need(id, "id", op), set_lead_to_completed=set_lead_to_completed)
            if op == "set_task_skipped":
                return client.skip_task(_need(id, "id", op))
            if op == "create_call_log":
                return client.log_call(**_need(call, "call", op))
            if op == "set_mailbox_daily_limit":
                return client.update_mailbox_daily_limit(
                    _need(id, "id", op), _need(daily_email_limit, "daily_email_limit", op))
            if op == "add_excluded_emails":
                return client.add_excluded_emails(_need(emails, "emails", op))
            if op == "add_excluded_domains":
                return client.add_excluded_domains(_need(domains, "domains", op))
            if op == "remove_excluded_emails":
                return client.remove_excluded_emails(_need(emails, "emails", op))
            if op == "remove_excluded_domains":
                return client.remove_excluded_domains(_need(domains, "domains", op))
            if op == "review_phone_number":
                return client.review_phone_number(
                    _need(id, "id", op), _need(user_id, "user_id", op))
            raise _bad(f"unknown op '{op}' — one of: {', '.join(_ACT_OPS)}")

        try:
            return _run(act)
        except TypeError as e:
            # `contact` / `call` dicts carry an unknown key: name it, nothing was sent.
            raise _bad(f"op='{op}': {e}. Nothing was done.")
