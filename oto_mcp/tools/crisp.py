"""Crisp tool — customer messaging, read-only.

Wraps `oto.tools.crisp.CrispClient` (REST API V1, Basic `token_identifier:token_key`
with `X-Crisp-Tier: website`), credential resolved per call via
`access.resolve_credential_fields("crisp")`: the website token and the website ID,
which every path carries.

The connector is read-only, so it serves ONE tool, `crisp` (LECTURE), the read verb
in `op` — no `crisp_act` (`tests/test_convention_act.py`). One op per function of the
`crisp` description (connectors repo, `connectors/crisp/connector.yaml`).

Personal data: conversations, messages and contacts may carry personal or health
data. The served text says so, and asks not to look a person up by name or phone
number unless the task requires it; nothing is masked by default.

Client calls are written out (`_client().list_conversations(…)`) so the
version-skew probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import LECTURE

_NAME = "crisp"

PRIVACY = ("May contain personal or health data: read only what the task needs, and do "
           "not look a person up by name or phone number unless the task requires it.")

_CONVERSATION_FILTERS = frozenset({
    "page", "per_page", "full", "inbox_id", "unread", "resolved", "assigned_to",
    "unassigned", "include_empty", "date_start", "date_end", "order"})
_PEOPLE_FILTERS = frozenset({"page", "per_page", "full", "sort_field", "sort_order",
                             "date_start", "date_end"})
_TREE_ARGS = ("locale", "content_type", "tree_path")
_TREE_ENTRY = frozenset(_TREE_ARGS)

#: What the default (non-`full`) list view drops from each row: what nobody reads to
#: choose which conversation or contact to open next — participants, mentions,
#: drafts, scheduled messages, verification details, presence; and a contact's
#: notepad, free text of the team that may hold sensitive notes. `full=True` returns
#: the raw rows, and the answer names what it left out.
_CONVERSATION_ROW_DROP = ("participants", "mentions", "compose", "scheduled_messages",
                          "verifications", "preview_message", "active", "availability")
_PERSON_ROW_DROP = ("notepad", "active", "score")
_HINT = "full=true returns the raw rows."

#: For each op, the arguments it reads (`op` aside), and those it requires.
_ACCEPTS: dict[str, frozenset] = {
    "website": frozenset(),
    "inboxes": frozenset({"page"}),
    "inbox": frozenset({"inbox_id"}),
    "conversations": _CONVERSATION_FILTERS,
    "search_conversations": _CONVERSATION_FILTERS | {"query", "search_type"},
    "conversation": frozenset({"session_id"}),
    "conversation_meta": frozenset({"session_id"}),
    "messages": frozenset({"session_id", "timestamp_before", "timestamp_after",
                           "timestamp_around"}),
    "people": _PEOPLE_FILTERS,
    "search_people": _PEOPLE_FILTERS | {"query"},
    "person": frozenset({"people_id"}),
    "person_data": frozenset({"people_id"}),
    "person_conversations": frozenset({"people_id", "page", "unread", "resolved"}),
    "helpdesk": frozenset(),
    "helpdesk_locales": frozenset({"page"}),
    "helpdesk_pages": frozenset({"locale", "content_type", "page"}),
    "helpdesk_tree": frozenset({"locale", "content_type", "page", "sub_path", "search_title",
                                "entry_status", "date_start", "date_end"}),
    "helpdesk_entry": _TREE_ENTRY,
    "helpdesk_content": _TREE_ENTRY,
    "helpdesk_metadata": _TREE_ENTRY,
}
_REQUIRES: dict[str, tuple] = {
    "inbox": ("inbox_id",),
    "search_conversations": ("query",),
    "conversation": ("session_id",), "conversation_meta": ("session_id",),
    "messages": ("session_id",),
    "search_people": ("query",),
    "person": ("people_id",), "person_data": ("people_id",),
    "person_conversations": ("people_id",),
    "helpdesk_pages": ("locale", "content_type"), "helpdesk_tree": ("locale", "content_type"),
    "helpdesk_entry": _TREE_ARGS, "helpdesk_content": _TREE_ARGS,
    "helpdesk_metadata": _TREE_ARGS,
}
_OPS = tuple(_ACCEPTS)
#: What a 404 means, by the object the op designates.
_NOT_FOUND = {
    "inbox": "no inbox with this inbox_id in this Crisp workspace",
    "conversation": "no conversation with this session_id in this Crisp workspace",
    "conversation_meta": "no conversation with this session_id in this Crisp workspace",
    "messages": "no conversation with this session_id in this Crisp workspace",
    "person": "no contact with this people_id or email in this Crisp workspace",
    "person_data": "no contact with this people_id or email in this Crisp workspace",
    "person_conversations": "no contact with this people_id or email in this Crisp workspace",
}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _check(op: str, given: dict) -> None:
    """No argument is silently dropped, a required one is named; nothing is called before."""
    stray = sorted(k for k, v in given.items() if v is not None and k not in _ACCEPTS[op])
    if stray:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                   "Nothing was done.")
    for name in _REQUIRES.get(op, ()):
        if given.get(name) in (None, ""):
            raise _bad(f"`{name}` is required for op='{op}'. Nothing was done.")


def _upstream_message(e, op: str) -> str:
    """Turn a Crisp refusal into an actionable message, by status."""
    status = e.status_code
    if status == 400:
        return f"Crisp rejected the request as invalid (400): check the arguments. {e.body}"
    if status == 401:
        return ("Crisp rejected the website token (401): unknown, or rolled since (rolling a "
                "token revokes the previous one).")
    if status == 402:
        return ("Crisp answered 402 Payment Required: the workspace's Crisp plan does not give "
                "access to this resource.")
    if status == 403:
        return ("Crisp refused the access (403): the token does not reach this workspace or "
                "this resource; check the website ID set on the connector.")
    if status == 404:
        what = _NOT_FOUND.get(op) or ("this Crisp workspace has no knowledge base, or not this "
                                      "entry" if op.startswith("helpdesk") else "check the id")
        return f"Crisp: not found (404) — {what}."
    if status == 416:
        return "Crisp answered 416: this page is out of range."
    if status == 423:
        return "Crisp answered 423 Locked for this resource."
    if status in (420, 429):
        return ("Crisp rate limit reached (a website token is limited to 10,000 requests per "
                "day): retry later.")
    if status >= 500:
        return f"Crisp is temporarily unavailable (HTTP {status}): retry later."
    return f"Crisp refused the request (HTTP {status}): {str(e.body)[:400]}"


def _view(result: Any, full: bool, drop: tuple) -> Any:
    """The default list view: each row of `data` without `drop`, the envelope kept, and
    what was left out named under `projection`. `full=True` returns the answer untouched."""
    if full or not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return result
    omitted = sorted({k for row in result["data"] if isinstance(row, dict)
                      for k in row if k in drop})
    out = output_projection.project(result, items_path="data", item_drop=drop)
    if omitted:
        out["projection"] = {"omitted": omitted, "hint": _HINT}
    return out


def _only(flag: Optional[bool]) -> Optional[int]:
    """A filter that narrows when true; false or unset sends nothing (no filter)."""
    return 1 if flag else None


def _resolution(resolved: Optional[bool]) -> dict:
    """`resolved` → the two exclusive flags of the API (unset = both states)."""
    return {"filter_resolved": 1 if resolved is True else None,
            "filter_not_resolved": 1 if resolved is False else None}


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: reads the workspace (`get_website`) — no side effect;
    401 on a wrong or rolled token, 403 when the token does not reach the website ID."""
    from oto.tools.crisp import CrispClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        answer = CrispClient(token_identifier=fields.get("token_identifier"),
                             token_key=fields.get("token_key"),
                             website_id=fields.get("website_id")).get_website()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Crisp HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Crisp HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))
    data = answer.get("data") if isinstance(answer, dict) else None
    if not (isinstance(data, dict) and data.get("website_id")):
        raise RuntimeError("Crisp answered without the workspace (data.website_id is empty).")


def register(mcp: FastMCP) -> None:
    from oto.tools.crisp import CrispClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> CrispClient:
        creds = access.resolve_credential_fields(_NAME)
        return CrispClient(token_identifier=creds.get("token_identifier"),
                           token_key=creds.get("token_key"),
                           website_id=creds.get("website_id"))

    def _run(op: str, fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e, op))

    @mcp.tool(name="crisp", annotations=LECTURE)
    def crisp(
        op: Literal[_OPS] = "conversations",
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        inbox_id: Optional[str] = None,
        session_id: Optional[str] = None,
        people_id: Optional[str] = None,
        query: Optional[str] = None,
        search_type: Optional[Literal["text", "segment"]] = None,
        unread: Optional[bool] = None,
        resolved: Optional[bool] = None,
        assigned_to: Optional[str] = None,
        unassigned: Optional[bool] = None,
        include_empty: Optional[bool] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        order: Optional[Literal["created", "updated", "waiting"]] = None,
        timestamp_before: Optional[int] = None,
        timestamp_after: Optional[int] = None,
        timestamp_around: Optional[int] = None,
        sort_field: Optional[str] = None,
        sort_order: Optional[Literal["ascending", "descending"]] = None,
        locale: Optional[str] = None,
        content_type: Optional[Literal["articles", "guides", "references", "news"]] = None,
        tree_path: Optional[str] = None,
        sub_path: Optional[str] = None,
        search_title: Optional[str] = None,
        entry_status: Optional[Literal["visible", "hidden", "draft", "featured"]] = None,
        full: bool = False,
    ) -> Any:
        """Crisp customer messaging, read-only: the workspace's conversations across its
        channels (chat, email and the others it connects) and their messages, its
        contacts, inboxes and knowledge base. Nothing is ever sent or changed in Crisp.
        May contain personal or health data: read only what the task needs, and do not
        look a person up by name or phone number unless the task requires it.

        `op`:
        - `conversations` — conversations by page: session_id, inbox_id, people_id,
          state (pending, unresolved, resolved), last_message (excerpt of the
          visitor's last message), assigned, unread, created_at, updated_at,
          waiting_since, meta {nickname, email, phone, origin, subject, segments}.
          The channel is `meta.origin` (chat, email or a urn): there is no channel
          filter, `inbox_id` narrows to one inbox. For incremental reading,
          `date_start`/`date_end` bound the UPDATE date and `order="updated"` sorts
          by it; no total nor next page is returned (page until a short page).
        - `search_conversations` — same, with `query`: a text searched in the
          conversations (`search_type` text, the default), or a segment name
          (`segment`). Search a topic or a segment, not a person.
        - `conversation` / `conversation_meta` — one conversation (`session_id`) / its
          metadata (channel, email, phone, custom data, device, segments).
        - `messages` — a batch of one conversation's messages, the latest without
          timestamp; `timestamp_before` pages to older ones (the oldest timestamp
          received), `timestamp_after` to newer ones, `timestamp_around` centres on
          one. Each has from (user = the contact, operator = the team), origin,
          type (`note` = internal note of the team), content, timestamp (ms). No
          field says whether a channel's messaging window (such as WhatsApp's 24
          hours) is open: the contact's last message is the latest with from user.
        - `people` / `search_people` — contacts by page (`query`: quick text search,
          prefer an id or an email received from the task); `date_start`/`date_end`
          bound the CREATION date of a profile.
        - `person` / `person_data` / `person_conversations` — one contact by
          `people_id` or email / its custom data / its conversations' session_id.
        - `inboxes` / `inbox` — the inboxes and their conditions (which
          conversations each gathers); an inbox_id filters `conversations`.
        - `website` — the workspace itself.
        - `helpdesk`, `helpdesk_locales` — the knowledge base and its locales;
          `helpdesk_pages` — published pages of a `locale` and `content_type`
          (tree_path, title, url); `helpdesk_tree` — the tree, drafts and hidden
          entries included; `helpdesk_entry` / `helpdesk_content` /
          `helpdesk_metadata` — one entry by `tree_path` / its body / its metadata.

        Answers are Crisp's own `{error, reason, data}`; timestamps are numbers. The
        conversation lists drop by default each row's participants, mentions, drafts,
        scheduled messages, verifications, preview and presence, the contact lists each
        contact's notepad, presence and score (named under `projection`); `full=true`
        returns raw rows.
        A website token is limited to 10,000 requests per day.

        Args:
            op: the read, see above (default conversations).
            page: lists — page number, from 1 (default 1).
            per_page: conversations / people — items per page, 20 to 50 (default 20).
            inbox_id: op='inbox' — the inbox; conversations — only this inbox.
            session_id: conversation / conversation_meta / messages — session_... id.
            people_id: person / person_data / person_conversations — people_id or email.
            query: search_conversations — text or segment name; search_people — quick search.
            search_type: search_conversations — text (default) | segment.
            unread: conversations / person_conversations — true: only with unread messages.
            resolved: conversations / person_conversations — true: only resolved,
                false: only not resolved; unset: both.
            assigned_to: conversations — only those assigned to this operator user id.
            unassigned: conversations — true: only those assigned to nobody.
            include_empty: conversations — true: also those without any message.
            date_start: from this date-time, ISO 8601 with offset
                (2026-03-01T00:00:00.000Z): conversations' update, contacts'
                creation, helpdesk_tree entries' update.
            date_end: up to this date-time, same formats and meaning.
            order: conversations — created | updated | waiting (longest waiting
                first, and only the waiting ones).
            timestamp_before: messages — batch before this timestamp (ms).
            timestamp_after: messages — batch after this timestamp (ms).
            timestamp_around: messages — batch centred on this timestamp (ms).
            sort_field: people — field to sort on, such as nickname.
            sort_order: people — ascending | descending.
            locale: helpdesk_* — locale code, such as en or fr (from helpdesk_locales).
            content_type: helpdesk_* — articles | guides | references | news.
            tree_path: helpdesk_entry / _content / _metadata — as helpdesk_pages returns it.
            sub_path: helpdesk_tree — directory to list from (default /).
            search_title: helpdesk_tree — text searched in the titles.
            entry_status: helpdesk_tree — visible | hidden | draft | featured.
            full: conversations / people lists — raw rows instead of the trimmed view.
        """
        given = dict(page=page, per_page=per_page, inbox_id=inbox_id, session_id=session_id,
                     people_id=people_id, query=query, search_type=search_type, unread=unread,
                     resolved=resolved, assigned_to=assigned_to, unassigned=unassigned,
                     include_empty=include_empty, date_start=date_start, date_end=date_end,
                     order=order, timestamp_before=timestamp_before,
                     timestamp_after=timestamp_after, timestamp_around=timestamp_around,
                     sort_field=sort_field, sort_order=sort_order, locale=locale,
                     content_type=content_type, tree_path=tree_path, sub_path=sub_path,
                     search_title=search_title, entry_status=entry_status,
                     full=full or None)
        if op not in _ACCEPTS:
            raise _bad(f"invalid `op`: {op!r} (expected: {' | '.join(_OPS)}).")
        _check(op, given)
        number = page if page is not None else 1
        conversation_filters = dict(
            per_page=per_page, include_empty=_only(include_empty), filter_inbox_id=inbox_id,
            filter_unread=_only(unread), filter_assigned=assigned_to,
            filter_unassigned=_only(unassigned),
            filter_date_start=date_start, filter_date_end=date_end,
            order_date_created=1 if order == "created" else None,
            order_date_updated=1 if order == "updated" else None,
            order_date_waiting=1 if order == "waiting" else None, **_resolution(resolved))
        people_filters = dict(per_page=per_page, sort_field=sort_field, sort_order=sort_order,
                              filter_date_start=date_start, filter_date_end=date_end)

        if op == "website":
            return _run(op, lambda: _client().get_website())
        if op == "inboxes":
            return _run(op, lambda: _client().list_inboxes(number))
        if op == "inbox":
            return _run(op, lambda: _client().get_inbox(inbox_id))
        if op == "conversations":
            return _view(_run(op, lambda: _client().list_conversations(
                number, **conversation_filters)), full, _CONVERSATION_ROW_DROP)
        if op == "search_conversations":
            return _view(_run(op, lambda: _client().search_conversations(
                number, query, search_type=search_type, **conversation_filters)),
                full, _CONVERSATION_ROW_DROP)
        if op == "conversation":
            return _run(op, lambda: _client().get_conversation(session_id))
        if op == "conversation_meta":
            return _run(op, lambda: _client().get_conversation_meta(session_id))
        if op == "messages":
            return _run(op, lambda: _client().list_conversation_messages(
                session_id, timestamp_before=timestamp_before,
                timestamp_after=timestamp_after, timestamp_around=timestamp_around))
        if op == "people":
            return _view(_run(op, lambda: _client().list_people(number, **people_filters)),
                         full, _PERSON_ROW_DROP)
        if op == "search_people":
            return _view(_run(op, lambda: _client().search_people(
                number, query, **people_filters)), full, _PERSON_ROW_DROP)
        if op == "person":
            return _run(op, lambda: _client().get_person(people_id))
        if op == "person_data":
            return _run(op, lambda: _client().get_person_data(people_id))
        if op == "person_conversations":
            return _run(op, lambda: _client().list_person_conversations(
                people_id, number, filter_unread=_only(unread), **_resolution(resolved)))
        if op == "helpdesk":
            return _run(op, lambda: _client().get_helpdesk())
        if op == "helpdesk_locales":
            return _run(op, lambda: _client().list_helpdesk_locales(number))
        if op == "helpdesk_pages":
            return _run(op, lambda: _client().list_helpdesk_pages(locale, content_type, number))
        if op == "helpdesk_tree":
            return _run(op, lambda: _client().list_helpdesk_tree(
                locale, content_type, number, sub_path=sub_path, search_title=search_title,
                search_filter_status=entry_status, filter_date_start=date_start,
                filter_date_end=date_end))
        if op == "helpdesk_entry":
            return _run(op, lambda: _client().get_helpdesk_entry(locale, content_type, tree_path))
        if op == "helpdesk_content":
            return _run(op, lambda: _client().get_helpdesk_content(locale, content_type, tree_path))
        return _run(op, lambda: _client().get_helpdesk_metadata(locale, content_type, tree_path))
