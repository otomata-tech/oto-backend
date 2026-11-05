"""Instantly tools — cold email outreach.

Wraps `oto.tools.instantly.InstantlyClient` (API V2, `Authorization: Bearer`), key
resolved per call via `access.resolve_api_key("instantly")`: the customer's own (user
or org), never a platform key. The connector serves its PAIR
(`tests/test_convention_act.py`):

- `instantly` (LECTURE) — the workspace, campaigns and their analytics, leads, lead
  lists and labels, the Unibox (emails, unread count), sending accounts, webhooks, the
  background job of a move;
- `instantly_act` (ACTE) — everything that changes Instantly: leads added, created,
  updated, moved, an interest status set, a lead list created, an address blocked, a
  campaign activated or paused, a reply SENT, a thread marked read, a webhook created
  or deleted.

The op vocabulary follows lemlist's where a gesture is the same, so a procedure ports
from one to the other: `campaigns`, `leads`, `lead`, `webhooks`; `add_leads`,
`update_lead`, `create_webhook`, `delete_webhook`.

**Scopes.** An Instantly key carries scopes chosen at its creation. A 403 is a missing
scope (or a plan without the feature), never a dead key: the refusal names the scope
the op needs (`_SCOPES`). A read-only key (`all:read`) serves `instantly` and refuses
every `instantly_act` op.

**What never reaches the agent**: a webhook's delivery `headers` (a secret) and an
email attachment's download `url` — the client strips both. Raw secrets never travel
as tool arguments either: `create_webhook` takes no `headers`.

Client calls are written out (`client.list_campaigns(…)`, the client bound by
`client = _client()`) so the version-skew probe (`test_tools_client_methods_exist`)
can check them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

_NAME = "instantly"

_READ_OPS = (
    "workspace", "campaigns", "campaign", "lead_campaigns", "campaign_analytics",
    "analytics_overview", "analytics_daily", "analytics_steps", "leads", "lead",
    "lead_lists", "lead_list", "lead_labels", "background_job", "emails", "email",
    "unread_count", "accounts", "account", "webhooks", "webhook_event_types",
)
_ACT_OPS = (
    "add_leads", "create_lead", "update_lead", "update_interest_status", "move_leads",
    "create_lead_list", "add_blocklist_entry", "activate_campaign", "set_campaign_paused",
    "reply_email", "set_thread_read", "create_webhook", "delete_webhook",
)

#: The scope each op needs, as Instantly spells it (`None`: the reference states none;
#: `all:read` / `all:all` cover it). Named in a 403 refusal.
_SCOPES: Dict[str, Optional[str]] = {
    "workspace": "workspaces:read", "campaigns": "campaigns:read", "campaign": "campaigns:read",
    "lead_campaigns": "campaigns:read", "campaign_analytics": None, "analytics_overview": None,
    "analytics_daily": None, "analytics_steps": None, "leads": "leads:read", "lead": "leads:read",
    "lead_lists": "lead_lists:read", "lead_list": "lead_lists:read",
    "lead_labels": "lead-labels:read", "background_job": "background-jobs:read",
    "emails": "emails:read", "email": "emails:read", "unread_count": "emails:read",
    "accounts": "accounts:read", "account": "accounts:read", "webhooks": None,
    "webhook_event_types": None, "add_leads": "leads:create", "create_lead": "leads:create",
    "update_lead": "leads:update", "update_interest_status": None, "move_leads": "leads:update",
    "create_lead_list": "lead_lists:create", "add_blocklist_entry": "block_list_entries:create",
    "activate_campaign": "campaigns:update", "set_campaign_paused": "campaigns:update",
    "reply_email": "emails:create", "set_thread_read": "emails:update",
    "create_webhook": "webhooks:create", "delete_webhook": "webhooks:delete",
}

_PAGE = frozenset({"limit", "starting_after"})
_RANGE = frozenset({"start_date", "end_date"})
_LEAD_FIELDS = frozenset({"first_name", "last_name", "company_name", "job_title", "phone",
                          "website", "personalization", "custom_variables"})
_SKIPS = frozenset({"skip_if_in_workspace", "skip_if_in_campaign", "skip_if_in_list",
                    "blocklist_id"})

#: For each op, the arguments it reads (`op` aside), and those it requires.
_ACCEPTS: Dict[str, frozenset] = {
    "workspace": frozenset(),
    "campaigns": _PAGE | {"search", "status", "tag_ids", "full"},
    "campaign": frozenset({"campaign_id"}),
    "lead_campaigns": frozenset({"email", "sort_order", "full"}),
    "campaign_analytics": _RANGE | {"campaign_id", "campaign_ids", "exclude_total_leads_count"},
    "analytics_overview": _RANGE | {"campaign_id", "campaign_ids", "campaign_status",
                                    "expand_crm_events"},
    "analytics_daily": _RANGE | {"campaign_id", "campaign_status"},
    "analytics_steps": _RANGE | {"campaign_id", "include_opportunities_count"},
    "leads": _PAGE | {"campaign_id", "list_id", "search", "contacts", "ids", "filter",
                      "in_campaign", "in_list", "distinct_contacts", "full"},
    "lead": frozenset({"lead_id"}),
    "lead_lists": _PAGE | {"search"},
    "lead_list": frozenset({"list_id"}),
    "lead_labels": _PAGE | {"search", "interest_status"},
    "background_job": frozenset({"job_id", "data_fields"}),
    "emails": _PAGE | {"campaign_id", "list_id", "lead", "search", "eaccount", "email_type",
                       "is_unread", "i_status", "tab", "latest_of_thread", "company_domain",
                       "since", "until", "sort_order", "full"},
    "email": frozenset({"email_id"}),
    "unread_count": frozenset(),
    "accounts": _PAGE | {"search", "status", "provider_code", "tag_ids", "full"},
    "account": frozenset({"email"}),
    "webhooks": _PAGE | {"campaign_id", "event_type"},
    "webhook_event_types": frozenset(),
}
_REQUIRES: Dict[str, tuple] = {
    "campaign": ("campaign_id",), "lead_campaigns": ("email",), "lead": ("lead_id",),
    "lead_list": ("list_id",), "background_job": ("job_id",), "email": ("email_id",),
    "account": ("email",),
}
_ACT_ACCEPTS: Dict[str, frozenset] = {
    "add_leads": _SKIPS | {"campaign_id", "list_id", "leads"},
    "create_lead": _SKIPS | _LEAD_FIELDS | {"campaign_id", "list_id", "email"},
    "update_lead": _LEAD_FIELDS | {"lead_id", "pl_value_lead"},
    "update_interest_status": frozenset({"email", "interest_value", "reset_interest",
                                         "campaign_id", "list_id"}),
    "move_leads": frozenset({"campaign_id", "list_id", "to_campaign_id", "to_list_id", "ids",
                             "contacts", "search", "filter", "max_leads", "copy_leads",
                             "check_duplicates", "check_duplicates_in_campaigns",
                             "skip_invalid_emails"}),
    "create_lead_list": frozenset({"name"}),
    "add_blocklist_entry": frozenset({"value"}),
    "activate_campaign": frozenset({"campaign_id"}),
    "set_campaign_paused": frozenset({"campaign_id"}),
    "reply_email": frozenset({"email_id", "eaccount", "subject", "text", "html", "cc", "bcc"}),
    "set_thread_read": frozenset({"thread_id"}),
    "create_webhook": frozenset({"url", "event_type", "custom_interest_value", "campaign_id",
                                 "name"}),
    "delete_webhook": frozenset({"webhook_id"}),
}
_ACT_REQUIRES: Dict[str, tuple] = {
    "add_leads": ("leads",), "create_lead": ("email",), "update_lead": ("lead_id",),
    "update_interest_status": ("email",), "create_lead_list": ("name",),
    "add_blocklist_entry": ("value",), "activate_campaign": ("campaign_id",),
    "set_campaign_paused": ("campaign_id",), "reply_email": ("email_id", "eaccount", "subject"),
    "set_thread_read": ("thread_id",), "create_webhook": ("url",),
    "delete_webhook": ("webhook_id",),
}

#: What the default (non-`full`) list views drop from each row: what nobody reads to
#: choose which record to open next. `full=True` returns the raw rows, and the answer
#: names what it left out.
_CAMPAIGN_ROW_DROP = ("sequences", "auto_variant_select", "provider_routing_rules",
                      "core_variables", "custom_variables", "limit_emails_per_company_override")
_LEAD_ROW_DROP = ("status_summary", "status_summary_subseq")
_ACCOUNT_ROW_DROP = ("warmup",)
_EMAIL_ROW_DROP = ("from_address_json", "to_address_json", "cc_address_json")
_HINT = "full=true returns the raw rows."


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _check(op: str, given: dict, accepts: dict, requires: dict) -> None:
    """No argument is silently dropped, a required one is named; nothing is called before."""
    stray = sorted(k for k, v in given.items() if v is not None and k not in accepts[op])
    if stray:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                   "Nothing was done.")
    for name in requires.get(op, ()):
        if given.get(name) in (None, "", []):
            raise _bad(f"`{name}` is required for op='{op}'. Nothing was done.")


def _upstream_message(e, op: str) -> str:
    """Turn an Instantly refusal into an actionable message, by status."""
    status = e.status_code
    detail = e.body.get("message") if isinstance(e.body, dict) else e.body
    detail = str(detail or "")[:300]
    if status == 400:
        return f"Instantly rejected the request as invalid (400): {detail}"
    if status == 401:
        return ("Instantly does not accept this API key (401): revoked or mistyped. Create "
                "one in Instantly → Settings → Integrations → API keys and set it again.")
    if status == 402:
        return ("Instantly refused the call (402): the workspace has no active paid plan "
                "(Instantly → Settings → Billing).")
    if status == 403:
        scope = _SCOPES.get(op)
        need = (f"`{scope}` (or `{scope.split(':')[0]}:all`, `all:all`)" if scope
                else "a scope Instantly does not document for this call (`all:all` covers it)")
        read_only = "" if op in _READ_OPS else " A read-only key refuses every action."
        return (f"Instantly refused op='{op}' (403): the API key lacks {need}, or the plan "
                f"excludes this feature.{read_only} Create a key with that scope in "
                "Instantly → Settings → Integrations → API keys.")
    if status == 404:
        return f"Instantly: not found in this workspace (404) — check the id. {detail}"
    if status == 409:
        return f"Instantly refused the change: it conflicts with the current state (409). {detail}"
    if status == 422:
        return f"Instantly could not process this request (422): {detail}"
    if status == 429:
        return ("Instantly rate limit reached (429; listing emails is limited to 20 requests "
                "per minute): retry in a minute.")
    if status >= 500:
        return (f"Instantly failed or timed out (HTTP {status}). An action may have gone "
                "through: check before repeating it.")
    return f"Instantly refused the request (HTTP {status}): {detail}"


def _view(result: Any, full: bool, drop: tuple, *, html: bool = False) -> Any:
    """The default list view: each row of `items` without `drop` (and, for emails, without
    `body.html`, the text and preview kept), the envelope kept, and what was left out named
    under `projection`. `full=True` returns the answer untouched."""
    if full or not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return result
    rows = [r for r in result["items"] if isinstance(r, dict)]
    omitted = sorted({k for row in rows for k in row if k in drop})
    out = output_projection.project(result, items_path="items", item_drop=drop)
    if html:
        trimmed = []
        for row in out["items"]:
            body = row.get("body") if isinstance(row, dict) else None
            if isinstance(body, dict) and "html" in body:
                row = {**row, "body": {k: v for k, v in body.items() if k != "html"}}
                if "body.html" not in omitted:
                    omitted.append("body.html")
            trimmed.append(row)
        out["items"] = trimmed
    if omitted:
        out["projection"] = {"omitted": omitted, "hint": _HINT}
    return out


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: reads the workspace (`get_workspace`) — no side effect;
    401 on a revoked or mistyped key, 403 when the key lacks `workspaces:read`."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.instantly import InstantlyClient

    try:
        answer = InstantlyClient(api_key=fields.get("key")).get_workspace()
    except UpstreamHTTPError as e:
        if e.status_code == 401:
            raise connector_verify.NonAutorise("Instantly HTTP 401: the API key is revoked or mistyped.")
        if e.status_code == 403:
            raise connector_verify.NonAutorise(
                "Instantly HTTP 403: the key lacks workspaces:read; create it with all:read "
                "(read only) or all:all (read and act).")
        raise RuntimeError(f"Instantly HTTP {e.status_code}: {e.body}")
    if not (isinstance(answer, dict) and answer.get("id")):
        raise RuntimeError("Instantly answered without the workspace (its id is empty).")


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.instantly import InstantlyClient

    connector_verify.register(_NAME, _verify)

    def _client() -> InstantlyClient:
        key, _ = access.resolve_api_key(_NAME)
        return InstantlyClient(api_key=key)

    def _run(op: str, fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e, op))

    @mcp.tool(name="instantly", annotations=LECTURE)
    def instantly(
        op: Literal[_READ_OPS],
        campaign_id: Optional[str] = None,
        campaign_ids: Optional[List[str]] = None,
        lead_id: Optional[str] = None,
        list_id: Optional[str] = None,
        email_id: Optional[str] = None,
        job_id: Optional[str] = None,
        email: Optional[str] = None,
        search: Optional[str] = None,
        status: Optional[int] = None,
        campaign_status: Optional[int] = None,
        tag_ids: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        exclude_total_leads_count: Optional[bool] = None,
        expand_crm_events: Optional[bool] = None,
        include_opportunities_count: Optional[bool] = None,
        filter: Optional[str] = None,
        contacts: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        in_campaign: Optional[bool] = None,
        in_list: Optional[bool] = None,
        distinct_contacts: Optional[bool] = None,
        interest_status: Optional[Literal["positive", "neutral", "negative"]] = None,
        data_fields: Optional[str] = None,
        lead: Optional[str] = None,
        eaccount: Optional[str] = None,
        email_type: Optional[Literal["received", "sent", "manual"]] = None,
        is_unread: Optional[bool] = None,
        i_status: Optional[int] = None,
        tab: Optional[Literal["emode_focused", "emode_others", "emode_all"]] = None,
        latest_of_thread: Optional[bool] = None,
        company_domain: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        provider_code: Optional[int] = None,
        event_type: Optional[str] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """Instantly cold email outreach, read-only: campaigns and their analytics, leads,
        lead lists and labels, the Unibox (replies), sending accounts, webhooks. Nothing is
        sent or changed — `instantly_act` acts.

        Lists page by cursor: `limit` (1-100), and `starting_after` = the previous
        answer's `next_starting_after` (absent = last page). Ids are UUIDs. Each op needs
        its key scope (named in a 403); a key with `all:read` reads everything.

        `op`:
        - `workspace` — the workspace the key reaches (and the connection check).
        - `campaigns` (`search` on the name, `status`: 0 draft, 1 active, 2 paused,
          3 completed, 4 running subsequences, -1 accounts unhealthy, -2 bounce protect,
          -99 suspended; `tag_ids` comma-separated) / `campaign` (`campaign_id`: in
          full, sequences = the email copy). `lead_campaigns` — the campaigns a lead
          (`email`) is in: check before adding someone again.
        - `campaign_analytics` — per-campaign totals (sent, contacted, opens, replies,
          clicks, bounces, unsubscribes, opportunities): `campaign_id`, `campaign_ids`
          or all; `start_date`/`end_date` (YYYY-MM-DD); `exclude_total_leads_count`
          answers much faster. `analytics_overview` — the same summed, interest totals
          too (`campaign_status`, `expand_crm_events`). `analytics_daily` — day by
          day. `analytics_steps` — by step and A/B variant
          (`include_opportunities_count`).
        - `leads` — by `campaign_id` or `list_id`, `search` (whole words of email,
          names, company), `contacts` (emails), `ids`, `filter` (FILTER_LEAD_INTERESTED,
          FILTER_VAL_REPLIED, FILTER_VAL_BOUNCED, FILTER_VAL_NOT_CONTACTED…),
          `in_campaign`, `in_list`, `distinct_contacts`. `lead` (`lead_id`): status (1
          active, 2 paused, 3 completed, -1 bounced, -2 unsubscribed, -3 skipped),
          lt_interest_status (1 interested, 2 meeting booked, 3 meeting completed, 4
          won, 0 out of office, -1 not interested, -2 wrong person, -3 lost, -4 no
          show), payload (custom variables).
        - `lead_lists` / `lead_list` (`list_id`); `lead_labels` — custom interest
          statuses and their number (`interest_status` positive|neutral|negative).
        - `emails` — the Unibox, newest first: `email_type=received` for replies,
          `is_unread`, `lead` (an email), `search` (an email or `thread:<thread_id>`),
          `campaign_id`, `eaccount` (sending account), `latest_of_thread`, `since` /
          `until` (creation date), `tab` (Unibox tab). Rows give body.text and
          content_preview; body.html is dropped unless `full=true`. Limited to 20
          requests per minute. `email` (`email_id`) — one in full. `unread_count`.
        - `accounts` — sending mailboxes: status (1 active, 2 paused, -1 connection
          error, -2 soft bounce, -3 sending error), warmup score, daily limit
          (`provider_code` 1 IMAP, 2 Google, 3 Microsoft). `account` (`email`).
        - `webhooks` (`campaign_id`, `event_type`) / `webhook_event_types`.
        - `background_job` (`job_id`, from `instantly_act op=move_leads`): status,
          progress, moved counts (`data_fields` keeps some keys of data).

        Lists drop by default what nobody reads to choose: campaigns' sequences and
        variant settings, leads' step summaries, accounts' warmup settings, emails'
        address blocks and HTML body (named under `projection`); `full=true` returns raw
        rows. A delivery header of a webhook and an attachment's download address never
        come back.

        Args:
            op: the read, see above.
            campaign_id: a campaign (filters leads, emails, webhooks, analytics).
            campaign_ids: campaign_analytics / analytics_overview — several campaigns.
            lead_id: op='lead'.
            list_id: op='lead_list'; leads / emails — only this lead list.
            email_id: op='email'.
            job_id: op='background_job'.
            email: lead_campaigns — the lead's email; account — the mailbox email.
            search: campaigns / lead_lists / lead_labels / accounts — name or email
                text; leads — whole words; emails — an email or thread:<thread_id>.
            status: campaigns / accounts — status number (see above).
            campaign_status: analytics_overview / analytics_daily — only this status.
            tag_ids: campaigns / accounts — comma-separated tag ids.
            start_date: analytics — from (YYYY-MM-DD or ISO 8601).
            end_date: analytics — to.
            exclude_total_leads_count: campaign_analytics — faster, without lead totals.
            expand_crm_events: analytics_overview — count every interest event.
            include_opportunities_count: analytics_steps — add opportunities per step.
            filter: leads — a lead state, FILTER_….
            contacts: leads — emails the leads have.
            ids: leads — lead ids.
            in_campaign: leads — in a campaign (true) or in none (false).
            in_list: leads — in a list (true) or in none (false).
            distinct_contacts: leads — one row per email (the cursor is then an email).
            interest_status: lead_labels — positive | neutral | negative.
            data_fields: background_job — comma-separated keys of data to keep.
            lead: emails — the lead's email.
            eaccount: emails — sending account email(s), comma-separated.
            email_type: emails — received | sent | manual.
            is_unread: emails — only unread (true).
            i_status: emails — interest status of the email.
            tab: emails — Unibox tab: emode_focused | emode_others | emode_all.
            latest_of_thread: emails — one email per thread.
            company_domain: emails — the lead's company domain.
            since: emails — created after (ISO 8601).
            until: emails — created before (ISO 8601).
            sort_order: emails / lead_campaigns — asc | desc.
            provider_code: accounts — 1 IMAP/SMTP, 2 Google, 3 Microsoft, 4 AWS.
            event_type: webhooks — reply_received, lead_interested, email_bounced…
            limit: lists — 1 to 100.
            starting_after: lists — the previous answer's next_starting_after.
            full: campaigns / lead_campaigns / leads / emails / accounts — raw rows.
        """
        given = dict(
            campaign_id=campaign_id, campaign_ids=campaign_ids, lead_id=lead_id, list_id=list_id,
            email_id=email_id, job_id=job_id, email=email, search=search, status=status,
            campaign_status=campaign_status, tag_ids=tag_ids, start_date=start_date,
            end_date=end_date, exclude_total_leads_count=exclude_total_leads_count,
            expand_crm_events=expand_crm_events,
            include_opportunities_count=include_opportunities_count, filter=filter,
            contacts=contacts, ids=ids, in_campaign=in_campaign, in_list=in_list,
            distinct_contacts=distinct_contacts, interest_status=interest_status,
            data_fields=data_fields, lead=lead, eaccount=eaccount, email_type=email_type,
            is_unread=is_unread, i_status=i_status, tab=tab, latest_of_thread=latest_of_thread,
            company_domain=company_domain, since=since, until=until, sort_order=sort_order,
            provider_code=provider_code, event_type=event_type, limit=limit,
            starting_after=starting_after, full=full or None)
        if op not in _ACCEPTS:
            raise _bad(f"invalid `op`: {op!r} (expected: {' | '.join(_READ_OPS)}).")
        _check(op, given, _ACCEPTS, _REQUIRES)
        page = dict(limit=limit, starting_after=starting_after)

        def call():
            client = _client()
            if op == "workspace":
                return client.get_workspace()
            if op == "campaigns":
                return _view(client.list_campaigns(search=search, status=status, tag_ids=tag_ids,
                                                   **page), full, _CAMPAIGN_ROW_DROP)
            if op == "campaign":
                return client.get_campaign(campaign_id)
            if op == "lead_campaigns":
                return _view(client.search_campaigns_by_lead(email, sort_order=sort_order),
                             full, _CAMPAIGN_ROW_DROP)
            if op == "campaign_analytics":
                return client.get_campaign_analytics(
                    campaign_id=campaign_id, campaign_ids=campaign_ids, start_date=start_date,
                    end_date=end_date, exclude_total_leads_count=exclude_total_leads_count)
            if op == "analytics_overview":
                return client.get_campaign_analytics_overview(
                    campaign_id=campaign_id, campaign_ids=campaign_ids, start_date=start_date,
                    end_date=end_date, campaign_status=campaign_status,
                    expand_crm_events=expand_crm_events)
            if op == "analytics_daily":
                return client.get_campaign_daily_analytics(
                    campaign_id=campaign_id, start_date=start_date, end_date=end_date,
                    campaign_status=campaign_status)
            if op == "analytics_steps":
                return client.get_campaign_steps_analytics(
                    campaign_id=campaign_id, start_date=start_date, end_date=end_date,
                    include_opportunities_count=include_opportunities_count)
            if op == "leads":
                return _view(client.list_leads(
                    campaign=campaign_id, list_id=list_id, search=search, contacts=contacts,
                    ids=ids, filter=filter, in_campaign=in_campaign, in_list=in_list,
                    distinct_contacts=distinct_contacts, **page), full, _LEAD_ROW_DROP)
            if op == "lead":
                return client.get_lead(lead_id)
            if op == "lead_lists":
                return client.list_lead_lists(search=search, **page)
            if op == "lead_list":
                return client.get_lead_list(list_id)
            if op == "lead_labels":
                return client.list_lead_labels(search=search, interest_status=interest_status,
                                               **page)
            if op == "background_job":
                return client.get_background_job(job_id, data_fields=data_fields)
            if op == "emails":
                return _view(client.list_emails(
                    campaign_id=campaign_id, list_id=list_id, lead=lead, search=search,
                    eaccount=eaccount, email_type=email_type, is_unread=is_unread,
                    i_status=i_status, mode=tab, latest_of_thread=latest_of_thread,
                    company_domain=company_domain, min_timestamp_created=since,
                    max_timestamp_created=until, sort_order=sort_order, **page),
                    full, _EMAIL_ROW_DROP, html=True)
            if op == "email":
                return client.get_email(email_id)
            if op == "unread_count":
                return client.count_unread_emails()
            if op == "accounts":
                return _view(client.list_accounts(search=search, status=status,
                                                  provider_code=provider_code, tag_ids=tag_ids,
                                                  **page), full, _ACCOUNT_ROW_DROP)
            if op == "account":
                return client.get_account(email)
            if op == "webhooks":
                return client.list_webhooks(campaign=campaign_id, event_type=event_type, **page)
            return client.list_webhook_event_types()

        return _run(op, call)

    @mcp.tool(name="instantly_act", annotations=ACTE)
    def instantly_act(
        op: Literal[_ACT_OPS],
        campaign_id: Optional[str] = None,
        list_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        leads: Optional[List[dict]] = None,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company_name: Optional[str] = None,
        job_title: Optional[str] = None,
        phone: Optional[str] = None,
        website: Optional[str] = None,
        personalization: Optional[str] = None,
        pl_value_lead: Optional[str] = None,
        custom_variables: Optional[dict] = None,
        skip_if_in_workspace: Optional[bool] = None,
        skip_if_in_campaign: Optional[bool] = None,
        skip_if_in_list: Optional[bool] = None,
        blocklist_id: Optional[str] = None,
        interest_value: Optional[int] = None,
        reset_interest: Optional[bool] = None,
        to_campaign_id: Optional[str] = None,
        to_list_id: Optional[str] = None,
        ids: Optional[List[str]] = None,
        contacts: Optional[List[str]] = None,
        search: Optional[str] = None,
        filter: Optional[str] = None,
        max_leads: Optional[int] = None,
        copy_leads: Optional[bool] = None,
        check_duplicates: Optional[bool] = None,
        check_duplicates_in_campaigns: Optional[bool] = None,
        skip_invalid_emails: Optional[bool] = None,
        name: Optional[str] = None,
        value: Optional[str] = None,
        email_id: Optional[str] = None,
        eaccount: Optional[str] = None,
        subject: Optional[str] = None,
        text: Optional[str] = None,
        html: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        thread_id: Optional[str] = None,
        url: Optional[str] = None,
        event_type: Optional[str] = None,
        custom_interest_value: Optional[int] = None,
        webhook_id: Optional[str] = None,
    ) -> Any:
        """Instantly — change the workspace: leads, campaigns, replies, the block list,
        webhooks. Each op needs its key scope (named in a 403): a read-only key refuses
        them all.

        `op`:
        - `add_leads` — ⚠️ up to 1000 `leads` into ONE `campaign_id` or ONE `list_id`,
          each {email, first_name, last_name, company_name, job_title, phone, website,
          personalization, custom_variables {name: text|number|true/false}}; in a
          campaign each lead needs an email and an ACTIVE campaign starts emailing them.
          `skip_if_in_workspace` / `skip_if_in_campaign` / `skip_if_in_list` skip leads
          already there (check `instantly op=lead_campaigns` first for one person).
          Answers counts (leads_uploaded, duplicated_leads, in_blocklist…) and
          created_leads [{index, id, email}].
        - `create_lead` — ⚠️ ONE lead (`email` + the same fields) in a `campaign_id` or a
          `list_id`; in an active campaign it is emailed.
        - `update_lead` — `lead_id` + the fields to change (others kept; the email
          cannot change); `custom_variables` are added to the lead's payload.
        - `update_interest_status` — ⚠️ a lead by `email`: `interest_value` (1
          interested, 2 meeting booked, 3 meeting completed, 4 won, 0 out of office, -1
          not interested, -2 wrong person, -3 lost, -4 no show, or a custom label's
          number) or `reset_interest=true` (back to Lead); `campaign_id` or `list_id`.
          Instantly may complete the lead, update an opportunity and start a
          subsequence, which emails.
        - `move_leads` — ⚠️ from ONE source (`campaign_id` or `list_id`) to ONE
          destination (`to_campaign_id` or `to_list_id`): all of it, or those matching
          `ids`, `contacts`, `search`, `filter`, at most `max_leads`; `copy_leads` copies.
          Runs in the background: read the job with `instantly op=background_job`.
        - `create_lead_list` — `name`; answers its id, for add_leads.
        - `add_blocklist_entry` — `value`: an email or a domain no campaign will email.
        - `activate_campaign` — ⚠️ `campaign_id`: it starts emailing its leads.
          `set_campaign_paused` — `campaign_id`: it stops sending.
        - `reply_email` — ⚠️ SENDS at once, cannot be recalled: reply to `email_id`
          (from `instantly op=emails`) from `eaccount` (a sending account), `subject`
          (usually "Re: …"), `text` and/or `html`, optional `cc` / `bcc`
          (comma-separated). The reply goes to the sender of that email.
        - `set_thread_read` — `thread_id`: every email of the thread marked read.
        - `create_webhook` — ⚠️ Instantly POSTs each event to `url` (https):
          `event_type` (reply_received, lead_interested, email_bounced, all_events…) or
          a custom label's `custom_interest_value`; one `campaign_id` or all. Each
          delivery carries lead data. `delete_webhook` — `webhook_id`.

        Args:
            op: the action, see above.
            campaign_id: the campaign (add_leads, create_lead, update_interest_status,
                activate_campaign, set_campaign_paused, create_webhook); move_leads — source.
            list_id: the lead list (add_leads, create_lead, update_interest_status);
                move_leads — source.
            lead_id: update_lead.
            leads: add_leads — the leads, 1 to 1000.
            email: create_lead — the lead's email; update_interest_status — the lead.
            first_name: create_lead / update_lead.
            last_name: create_lead / update_lead.
            company_name: create_lead / update_lead.
            job_title: create_lead / update_lead.
            phone: create_lead / update_lead.
            website: create_lead / update_lead.
            personalization: create_lead / update_lead — the {{personalization}} line.
            pl_value_lead: update_lead — potential value.
            custom_variables: create_lead / update_lead — {name: text|number|bool}.
            skip_if_in_workspace: add_leads / create_lead — skip a lead already anywhere.
            skip_if_in_campaign: add_leads / create_lead — skip one in any campaign.
            skip_if_in_list: add_leads / create_lead — skip one in any list.
            blocklist_id: add_leads / create_lead — the block list to check (default:
                the workspace's).
            interest_value: update_interest_status — the status number.
            reset_interest: update_interest_status — true: back to Lead.
            to_campaign_id: move_leads — destination campaign.
            to_list_id: move_leads — destination list.
            ids: move_leads — lead ids.
            contacts: move_leads — lead emails.
            search: move_leads — whole words of email, names, company.
            filter: move_leads — a lead state, FILTER_….
            max_leads: move_leads — at most this many leads.
            copy_leads: move_leads — copy instead of move.
            check_duplicates: move_leads — skip leads already in the destination.
            check_duplicates_in_campaigns: move_leads — skip leads in any campaign.
            skip_invalid_emails: move_leads — leave invalid and risky emails behind.
            name: create_lead_list / create_webhook — a name.
            value: add_blocklist_entry — an email or a domain.
            email_id: reply_email — the email replied to.
            eaccount: reply_email — the sending account.
            subject: reply_email.
            text: reply_email — plain text body.
            html: reply_email — HTML body (line breaks as <br/>).
            cc: reply_email — comma-separated emails.
            bcc: reply_email — comma-separated emails.
            thread_id: set_thread_read.
            url: create_webhook — the https address events are posted to.
            event_type: create_webhook — the event listened to.
            custom_interest_value: create_webhook — a custom label's number.
            webhook_id: delete_webhook.
        """
        given = dict(
            campaign_id=campaign_id, list_id=list_id, lead_id=lead_id, leads=leads, email=email,
            first_name=first_name, last_name=last_name, company_name=company_name,
            job_title=job_title, phone=phone, website=website, personalization=personalization,
            pl_value_lead=pl_value_lead, custom_variables=custom_variables,
            skip_if_in_workspace=skip_if_in_workspace, skip_if_in_campaign=skip_if_in_campaign,
            skip_if_in_list=skip_if_in_list, blocklist_id=blocklist_id,
            interest_value=interest_value, reset_interest=reset_interest or None,
            to_campaign_id=to_campaign_id, to_list_id=to_list_id, ids=ids, contacts=contacts,
            search=search, filter=filter, max_leads=max_leads, copy_leads=copy_leads,
            check_duplicates=check_duplicates,
            check_duplicates_in_campaigns=check_duplicates_in_campaigns,
            skip_invalid_emails=skip_invalid_emails, name=name, value=value, email_id=email_id,
            eaccount=eaccount, subject=subject, text=text, html=html, cc=cc, bcc=bcc,
            thread_id=thread_id, url=url, event_type=event_type,
            custom_interest_value=custom_interest_value, webhook_id=webhook_id)
        if op not in _ACT_ACCEPTS:
            raise _bad(f"invalid `op`: {op!r} (expected: {' | '.join(_ACT_OPS)}).")
        _check(op, given, _ACT_ACCEPTS, _ACT_REQUIRES)
        if op == "update_interest_status" and (interest_value is None) == (not reset_interest):
            raise _bad("op='update_interest_status' takes `interest_value`, or "
                       "`reset_interest=true` to reset the lead to Lead — one of the two. "
                       "Nothing was done.")
        fields = dict(first_name=first_name, last_name=last_name, company_name=company_name,
                      job_title=job_title, phone=phone, website=website,
                      personalization=personalization, custom_variables=custom_variables)
        skips = dict(blocklist_id=blocklist_id, skip_if_in_workspace=skip_if_in_workspace,
                     skip_if_in_campaign=skip_if_in_campaign, skip_if_in_list=skip_if_in_list)

        def act():
            client = _client()
            if op == "add_leads":
                return client.add_leads(leads, campaign_id=campaign_id, list_id=list_id, **skips)
            if op == "create_lead":
                return client.create_lead(email, campaign=campaign_id, list_id=list_id,
                                          **fields, **skips)
            if op == "update_lead":
                return client.update_lead(lead_id, pl_value_lead=pl_value_lead, **fields)
            if op == "update_interest_status":
                return client.update_lead_interest_status(
                    email, None if reset_interest else interest_value,
                    campaign_id=campaign_id, list_id=list_id)
            if op == "move_leads":
                return client.move_leads(
                    campaign=campaign_id, list_id=list_id, to_campaign_id=to_campaign_id,
                    to_list_id=to_list_id, ids=ids, contacts=contacts, search=search,
                    filter=filter, limit=max_leads, copy_leads=copy_leads,
                    check_duplicates=check_duplicates,
                    check_duplicates_in_campaigns=check_duplicates_in_campaigns,
                    skip_invalid_emails=skip_invalid_emails)
            if op == "create_lead_list":
                return client.create_lead_list(name)
            if op == "add_blocklist_entry":
                return client.add_block_list_entry(value)
            if op == "activate_campaign":
                return client.activate_campaign(campaign_id)
            if op == "set_campaign_paused":
                return client.pause_campaign(campaign_id)
            if op == "reply_email":
                return client.reply_to_email(email_id, eaccount, subject, text=text, html=html,
                                             cc_address_email_list=cc,
                                             bcc_address_email_list=bcc)
            if op == "set_thread_read":
                return client.mark_thread_as_read(thread_id)
            if op == "create_webhook":
                return client.create_webhook(url, event_type=event_type,
                                             custom_interest_value=custom_interest_value,
                                             campaign=campaign_id, name=name)
            return client.delete_webhook(webhook_id)

        return _run(op, act)
