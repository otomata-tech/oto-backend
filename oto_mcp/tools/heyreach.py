"""HeyReach tools — LinkedIn outreach automation.

Wraps `oto.tools.heyreach.HeyReachClient` (public API, `X-API-KEY`), key resolved per
call via `access.resolve_api_key("heyreach")`: the customer's own workspace key (user or
org). The connector serves its PAIR (`tests/test_convention_act.py`), one op per
function of the `heyreach` description (connectors repo,
`connectors/heyreach/connector.yaml`):

- `heyreach` (LECTURE) — the key check, campaigns and the leads they work on, the
  campaigns and lists of a lead, lead lists and their leads, a lead and its tags,
  statistics, the LinkedIn inbox (GetConversationsV3) and one thread, the sender
  accounts, whether a lead is a connection, the webhooks;
- `heyreach_act` (ACTE) — add leads to a campaign or a list, stop a lead in a
  campaign, pause or resume a campaign, create a list, send a message, mark a
  conversation read or unread, add or replace a lead's tags, create or delete a
  webhook.

The arguments are the agent's: a LinkedIn sender account is `sender_id` everywhere
(`linkedInAccountId`, `accountId`, `senderAccountId` at HeyReach), a period is
`date_from` / `date_to` whatever date it bounds, a lead is `profile_url` or
`linkedin_id`. Offset lists take a default `limit` (the description requires one).

Lists come back trimmed by default (the heavy blocks nobody reads to choose the next
row, named under `projection`); `full=true` returns the raw rows. A member's picture
and a webhook's custom headers never come back (stripped by the client).

Client calls are written out (`client.list_campaigns(…)`, the client bound by
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

_NAME = "heyreach"

_READ_OPS = (
    "check_key", "campaigns", "campaign", "campaign_leads", "lead_campaigns", "lists",
    "list", "list_leads", "lead_lists", "lead", "lead_tags", "stats", "stats_by_campaign",
    "conversations", "conversation", "senders", "sender", "is_connection", "webhooks",
    "webhook",
)
_ACT_OPS = (
    "add_campaign_leads", "set_campaign_lead_stopped", "set_campaign_paused",
    "set_campaign_resumed", "add_list_leads", "create_list", "send_message",
    "set_conversation_seen", "add_lead_tags", "set_lead_tags", "create_webhook",
    "delete_webhook",
)
_EVENTS = ("CONNECTION_REQUEST_SENT", "CONNECTION_REQUEST_ACCEPTED", "MESSAGE_SENT",
           "MESSAGE_REPLY_RECEIVED", "INMAIL_SENT", "INMAIL_REPLY_RECEIVED",
           "EVERY_MESSAGE_REPLY_RECEIVED", "FOLLOW_SENT", "LIKED_POST", "VIEWED_PROFILE",
           "CAMPAIGN_COMPLETED", "LEAD_TAG_UPDATED")

#: The `limit` an offset list takes when the agent gives none.
_DEFAULT_LIMIT = 50

#: What the default (non-`full`) view drops, row by row: the exclusion settings of a
#: campaign, a lead's long prose and audience counters, a conversation's whole thread
#: and its sender block (`linkedInAccountId` stays; the thread is op='conversation'), a
#: sender's daily limits. `full=True` returns the raw rows.
_CAMPAIGN_DROP = ("excludeAlreadyMessagedGlobal", "excludeAlreadyMessagedCampaignAccounts",
                  "excludeFirstConnectionCampaignAccounts", "excludeFirstConnectionGlobal",
                  "excludeNoProfilePicture", "excludeListId", "excludeInOtherCampaigns",
                  "excludeHasOtherAccConversations",
                  "excludeContactedFromSenderInOtherCampaign")
_PROFILE_DROP = ("about", "summary", "connections", "followers")
_CONVERSATION_DROP = ("messages", "linkedInAccount")
_SENDER_DROP = ("accountLimits",)
_HINT = "full=true returns the raw rows."

#: What a 404 means, by the object the op designates.
_NOT_FOUND = {
    "campaign": "no campaign with this campaign_id in this HeyReach workspace",
    "list": "no list with this list_id in this HeyReach workspace",
    "sender": "no LinkedIn sender account with this sender_id in this workspace",
    "conversation": "no conversation with this conversation_id for this sender_id",
    "webhook": "no webhook with this webhook_id",
    "lead": "no lead with this profile URL in this workspace",
}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value: Any, name: str, op: str) -> Any:
    if value is None or value == "" or value == []:
        raise _bad(f"`{name}` is required for op='{op}'. Nothing was done.")
    return value


def _detail(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("errorMessage") or body.get("detail") or body.get("title") or body)[:400]
    return str(body or "")[:400]


def _upstream_message(e, op: str) -> str:
    """Turn a HeyReach refusal into an actionable message, by status."""
    status = e.status_code
    if status == 400:
        return f"HeyReach rejected the request (400): {_detail(e.body)}"
    if status == 401:
        return ("HeyReach refused the API key (401): unknown, deleted or deactivated — "
                "check the key set on this connector (HeyReach → Integrations).")
    if status == 403:
        return (f"HeyReach refused this action for this key (403): {_detail(e.body)} — the "
                "sender account may not hold this conversation.")
    if status == 404:
        return f"HeyReach: not found (404) — {_NOT_FOUND.get(op, 'check the id')}."
    if status == 429:
        return ("HeyReach rate limit reached (300 requests per minute for the whole "
                "workspace): wait, then retry.")
    if status >= 500:
        return (f"HeyReach failed (HTTP {status}). A read can be retried; an add, a send or a "
                "change may have been applied: read it back before acting again.")
    return f"HeyReach refused the request (HTTP {status}): {_detail(e.body)}"


def _slim_rows(result: Any, drop: tuple = (), nested: Optional[tuple] = None) -> Any:
    """The default list view: each row of `items` without `drop`, and, with `nested`
    (`key`, `keys`), the object under `key` without `keys`; the envelope (totalCount,
    nextCursor, hasNextPage) kept, what was left out named under `projection`."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return result
    rows = result["items"]
    omitted = {k for row in rows if isinstance(row, dict) for k in row if k in drop}
    out = output_projection.project(result, items_path="items", item_drop=drop)
    if nested:
        key, keys = nested
        trimmed = []
        for row in out["items"]:
            inner = row.get(key) if isinstance(row, dict) else None
            if isinstance(inner, dict):
                omitted |= {f"{key}.{k}" for k in inner if k in keys}
                row = {**row, key: {k: v for k, v in inner.items() if k not in keys}}
            trimmed.append(row)
        out["items"] = trimmed
    if omitted:
        out["projection"] = {"omitted": sorted(omitted), "hint": _HINT}
    return out


def _view(op: str, result: Any, full: bool) -> Any:
    if full:
        return result
    if op == "campaigns":
        return _slim_rows(result, _CAMPAIGN_DROP)
    if op == "campaign_leads":
        return _slim_rows(result, nested=("linkedInUserProfile", _PROFILE_DROP))
    if op == "list_leads":
        return _slim_rows(result, _PROFILE_DROP)
    if op == "conversations":
        return _slim_rows(result, _CONVERSATION_DROP, ("correspondentProfile", _PROFILE_DROP))
    if op == "senders":
        return _slim_rows(result, _SENDER_DROP)
    return result


def _pairs(leads: List[dict], sender_id: Optional[int]) -> List[dict]:
    """`leads` (each may carry its own `linkedInAccountId`) → HeyReach's account/lead
    pairs; `sender_id` is the sender of every lead that names none."""
    pairs = []
    for i, lead in enumerate(leads):
        if not isinstance(lead, dict):
            raise _bad(f"`leads`[{i}] must be an object with HeyReach's lead keys "
                       "(profileUrl, firstName…). Nothing was done.")
        lead = dict(lead)
        sender = lead.pop("linkedInAccountId", None) or sender_id
        pairs.append({"lead": lead, **({"linkedInAccountId": sender} if sender else {})})
    return pairs


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: `check_api_key` — no side effect, 401 on a key that is
    unknown, deleted or deactivated."""
    from oto.tools.heyreach import HeyReachClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        HeyReachClient(api_key=fields.get("key")).check_api_key()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"HeyReach HTTP {e.status_code}: {_detail(e.body)}")
        raise RuntimeError(f"HeyReach HTTP {e.status_code}: {_detail(e.body)}")


def register(mcp: FastMCP) -> None:
    from oto.tools.heyreach import HeyReachClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> HeyReachClient:
        key, _ = access.resolve_api_key(_NAME)
        return HeyReachClient(api_key=key)

    def _run(op: str, fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(f"{e} Nothing was done.")
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e, op))

    @mcp.tool(name="heyreach", annotations=LECTURE)
    def heyreach(
        op: Literal[_READ_OPS],
        campaign_id: Optional[int] = None,
        campaign_ids: Optional[List[int]] = None,
        list_id: Optional[int] = None,
        sender_id: Optional[int] = None,
        sender_ids: Optional[List[int]] = None,
        conversation_id: Optional[str] = None,
        webhook_id: Optional[int] = None,
        profile_url: Optional[str] = None,
        linkedin_id: Optional[str] = None,
        email: Optional[str] = None,
        keyword: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        list_type: Optional[Literal["USER_LIST", "COMPANY_LIST"]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        time_filter: Optional[Literal["CreationTime", "Everywhere", "LastActionTakenTime",
                                      "FailedTime", "LastActionTakenOrFailedTime"]] = None,
        search: Optional[str] = None,
        tags: Optional[List[str]] = None,
        auto_tags: Optional[List[str]] = None,
        seen: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        cursor: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """HeyReach — read LinkedIn outreach: campaigns, leads, lists, inbox, stats.

        Nothing is sent or changed here (that is `heyreach_act`). Leads and conversations
        carry personal data: read only what the task needs. A LinkedIn sender account is
        `sender_id`; a lead is `profile_url` (https://www.linkedin.com/in/<slug>/) or
        `linkedin_id` (linkedin_id in answers). Offset lists answer `totalCount`: next
        page = offset + limit (default limit 50). Lists drop heavy blocks by default
        (named under `projection`): `full=true` returns the raw rows.

        Ops:
        - `check_key` — whether the API key works.
        - `campaigns` (`keyword`, `statuses` DRAFT/IN_PROGRESS/PAUSED/FINISHED/CANCELED/
          FAILED/STARTING/SCHEDULED, `sender_ids`) / `campaign` (`campaign_id`): status,
          the lead list it draws from, its senders, progressStats.
        - `campaign_leads` (`campaign_id`, `time_filter` + `date_from`/`date_to`, limit
          ≤ 100): the leads it works on, with leadCampaignStatus, leadConnectionStatus,
          leadMessageStatus, the failure reason and the sender.
        - `lead_campaigns` / `lead_lists` (`profile_url`, `linkedin_id` or `email`): where
          a lead is.
        - `lists` (`keyword`, `list_type`, `campaign_ids`) / `list` (`list_id`).
        - `list_leads` (`list_id`, `keyword`, `profile_url`/`linkedin_id`, created
          `date_from`/`date_to`, limit ≤ 1000).
        - `lead` (`profile_url`): the lead's profile and emails; `lead_tags` (`profile_url`).
        - `stats` / `stats_by_campaign` (`date_from`, `date_to` required; `sender_ids`,
          `campaign_ids`, empty = all): connections sent/accepted, messages, replies,
          InMails, rates as fractions, overall and per day.
        - `conversations` — the LinkedIn inbox, newest first (`seen=false`: unread;
          `campaign_ids`, `sender_ids`, `search`, `tags`, `auto_tags`, one lead by
          `profile_url` or `linkedin_id`, last message `date_from`/`date_to`; limit
          ≤ 100, default 10): next page = `cursor` = nextCursor while hasNextPage. Rows
          drop their thread by default; `conversation` (`sender_id` = linkedInAccountId,
          `conversation_id` = id) gives it whole.
        - `senders` (`keyword`) / `sender` (`sender_id`): the LinkedIn sender accounts,
          authIsValid false = session to reconnect in HeyReach.
        - `is_connection` (`sender_id`, `profile_url` or `linkedin_id`).
        - `webhooks` / `webhook` (`webhook_id`): name, url, eventType, campaignIds.

        HeyReach allows 300 requests per minute for the whole workspace.
        """
        page = dict(limit=limit if limit is not None else _DEFAULT_LIMIT, offset=offset)

        def call():
            client = _client()
            if op == "check_key":
                answer = client.check_api_key()
                return {"ok": True, **(answer if isinstance(answer, dict) else {})}
            if op == "campaigns":
                return client.list_campaigns(keyword=keyword, statuses=statuses,
                                             account_ids=sender_ids, **page)
            if op == "campaign":
                return client.get_campaign(_need(campaign_id, "campaign_id", op))
            if op == "campaign_leads":
                return client.list_campaign_leads(
                    _need(campaign_id, "campaign_id", op), time_from=date_from,
                    time_to=date_to, time_filter=time_filter, **page)
            if op == "lead_campaigns":
                return client.list_lead_campaigns(profile_url=profile_url,
                                                  linkedin_id=linkedin_id, email=email, **page)
            if op == "lists":
                return client.list_lists(keyword=keyword, list_type=list_type,
                                         campaign_ids=campaign_ids, **page)
            if op == "list":
                return client.get_list(_need(list_id, "list_id", op))
            if op == "list_leads":
                return client.list_list_leads(
                    _need(list_id, "list_id", op), keyword=keyword,
                    lead_profile_url=profile_url, lead_linkedin_id=linkedin_id,
                    created_from=date_from, created_to=date_to, **page)
            if op == "lead_lists":
                return client.list_lead_lists(profile_url=profile_url,
                                              linkedin_id=linkedin_id, email=email, **page)
            if op == "lead":
                return client.get_lead(_need(profile_url, "profile_url", op))
            if op == "lead_tags":
                return client.get_lead_tags(_need(profile_url, "profile_url", op))
            if op == "stats":
                return client.get_overall_stats(
                    _need(date_from, "date_from", op), _need(date_to, "date_to", op),
                    account_ids=sender_ids or [], campaign_ids=campaign_ids or [])
            if op == "stats_by_campaign":
                return client.get_stats_by_campaign(
                    _need(date_from, "date_from", op), _need(date_to, "date_to", op),
                    account_ids=sender_ids or [], campaign_ids=campaign_ids or [])
            if op == "conversations":
                return client.list_conversations(
                    limit, cursor, last_message_from=date_from, last_message_to=date_to,
                    linkedin_account_ids=sender_ids, campaign_ids=campaign_ids,
                    search=search, lead_linkedin_id=linkedin_id,
                    lead_profile_url=profile_url, tags=tags,
                    latest_auto_tag_names=auto_tags, seen=seen)
            if op == "conversation":
                return client.get_conversation(_need(sender_id, "sender_id", op),
                                               _need(conversation_id, "conversation_id", op))
            if op == "senders":
                return client.list_linkedin_accounts(keyword=keyword, **page)
            if op == "sender":
                return client.get_linkedin_account(_need(sender_id, "sender_id", op))
            if op == "is_connection":
                return client.is_connection(_need(sender_id, "sender_id", op),
                                            lead_profile_url=profile_url,
                                            lead_linkedin_id=linkedin_id)
            if op == "webhooks":
                return client.list_webhooks(**page)
            if op == "webhook":
                return client.get_webhook(_need(webhook_id, "webhook_id", op))
            raise _bad(f"unknown op '{op}' — one of: {', '.join(_READ_OPS)}")

        return _view(op, _run(op, call), full)

    @mcp.tool(name="heyreach_act", annotations=ACTE)
    def heyreach_act(
        op: Literal[_ACT_OPS],
        campaign_id: Optional[int] = None,
        campaign_ids: Optional[List[int]] = None,
        list_id: Optional[int] = None,
        sender_id: Optional[int] = None,
        conversation_id: Optional[str] = None,
        webhook_id: Optional[int] = None,
        leads: Optional[List[dict]] = None,
        profile_url: Optional[str] = None,
        linkedin_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        create_missing_tags: Optional[bool] = None,
        resume_paused: Optional[bool] = None,
        resume_finished: Optional[bool] = None,
        name: Optional[str] = None,
        list_type: Optional[Literal["USER_LIST", "COMPANY_LIST"]] = None,
        message: Optional[str] = None,
        subject: Optional[str] = None,
        seen: Optional[bool] = None,
        webhook_url: Optional[str] = None,
        event_type: Optional[Literal[_EVENTS]] = None,
    ) -> Any:
        """HeyReach — act on LinkedIn outreach: leads, campaigns, messages, tags, webhooks.

        A lead in `leads` uses HeyReach's own keys: profileUrl (required), firstName,
        lastName, location, summary, companyName, position, about, emailAddress,
        customUserFields [{name, value}] (names: letters, digits, _), at most 100 per
        call. A lead is otherwise `profile_url` or `linkedin_id`; a LinkedIn sender
        account is `sender_id`.

        Ops:
        - ⚠️ `add_campaign_leads` — `campaign_id` + `leads` (each may carry its own
          `linkedInAccountId`; `sender_id` = the sender of the others, otherwise the
          campaign distributes them): an IN_PROGRESS campaign starts connecting with
          and messaging them. `resume_paused` / `resume_finished` restart the campaign.
          Answers addedLeadsCount, updatedLeadsCount, failedLeadsCount.
        - `set_campaign_lead_stopped` — `campaign_id` + `profile_url` or `linkedin_id`:
          no further step runs for this lead (cannot be undone here).
        - `set_campaign_paused` / ⚠️ `set_campaign_resumed` — `campaign_id`: resuming
          starts contacting its leads again.
        - `add_list_leads` — `list_id` + `leads`: a running campaign that uses the list
          may pick them up. `create_list` — `name`, `list_type` (USER_LIST default).
        - ⚠️ `send_message` — `sender_id` + `conversation_id` (linkedInAccountId and id
          from heyreach op='conversations') + `message`, `subject` for an InMail thread:
          reaches the person at once, cannot be unsent.
        - `set_conversation_seen` — `sender_id` + `conversation_id` + `seen` (false:
          mark unread).
        - `add_lead_tags` / ⚠️ `set_lead_tags` (replaces every tag) — `profile_url` or
          `linkedin_id` + `tags`; `create_missing_tags=true` creates unknown tags
          (otherwise refused).
        - ⚠️ `create_webhook` — `name`, `webhook_url` (https), `event_type` (one per
          webhook: EVERY_MESSAGE_REPLY_RECEIVED for every reply, MESSAGE_REPLY_RECEIVED
          for the first, CONNECTION_REQUEST_ACCEPTED…), `campaign_ids` (empty: all):
          HeyReach POSTs each event, with the lead's data, to that address — the way to
          wake an agent on a reply. `delete_webhook` — `webhook_id`.
        """
        def act():
            client = _client()
            if op == "add_campaign_leads":
                return client.add_leads_to_campaign(
                    _need(campaign_id, "campaign_id", op),
                    _pairs(_need(leads, "leads", op), sender_id),
                    resume_paused_campaign=resume_paused,
                    resume_finished_campaign=resume_finished)
            if op == "set_campaign_lead_stopped":
                return client.stop_lead_in_campaign(
                    _need(campaign_id, "campaign_id", op), lead_member_id=linkedin_id,
                    lead_url=profile_url)
            if op == "set_campaign_paused":
                return client.pause_campaign(_need(campaign_id, "campaign_id", op))
            if op == "set_campaign_resumed":
                return client.resume_campaign(_need(campaign_id, "campaign_id", op))
            if op == "add_list_leads":
                return client.add_leads_to_list(_need(list_id, "list_id", op),
                                                _need(leads, "leads", op))
            if op == "create_list":
                return client.create_empty_list(_need(name, "name", op), list_type)
            if op == "send_message":
                return client.send_message(
                    _need(sender_id, "sender_id", op),
                    _need(conversation_id, "conversation_id", op),
                    _need(message, "message", op), subject=subject)
            if op == "set_conversation_seen":
                if seen is None:
                    raise _bad("`seen` is required for op='set_conversation_seen' (true: read, "
                               "false: unread). Nothing was done.")
                return client.set_seen_status(_need(sender_id, "sender_id", op),
                                              _need(conversation_id, "conversation_id", op),
                                              seen)
            if op == "add_lead_tags":
                return client.add_lead_tags(
                    _need(tags, "tags", op), lead_profile_url=profile_url,
                    lead_linkedin_id=linkedin_id, create_tag_if_not_existing=create_missing_tags)
            if op == "set_lead_tags":
                return client.replace_lead_tags(
                    _need(tags, "tags", op), lead_profile_url=profile_url,
                    lead_linkedin_id=linkedin_id, create_tag_if_not_existing=create_missing_tags)
            if op == "create_webhook":
                return client.create_webhook(
                    _need(name, "name", op), _need(webhook_url, "webhook_url", op),
                    _need(event_type, "event_type", op), campaign_ids=campaign_ids or [])
            if op == "delete_webhook":
                return client.delete_webhook(_need(webhook_id, "webhook_id", op))
            raise _bad(f"unknown op '{op}' — one of: {', '.join(_ACT_OPS)}")

        return _run(op, act)
