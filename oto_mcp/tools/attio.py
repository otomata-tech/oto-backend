"""Attio CRM — full CRUD on records + notes/tasks/lists/entries/comments/meta.

Covers create/read/update/delete on the records of any object of the workspace
(companies, people, deals and custom objects), and
create/list/delete for notes (the Attio API does not allow editing a note's
body), and create/list/update/delete for tasks (update limited to
`deadline_at`, `is_completed`, `linked_records`, `assignees` on the API side).

Key resolved per call via `access.resolve_api_key("attio")`. Since Attio
has no default quota (see `access._QUOTA_DEFAULTS`), only
admins (with a server `ATTIO_API_KEY`) or users with their own
key set on `/account` can call these tools.

**Consolidated surface (ADR 0047 §Amendment, applied to the attio connector)**: one
tool per business OBJECT, the verb in the `op` parameter — 56 tools → 10. Attio is an
OBJECT-BASED CRM: the `create_X`/`get_X`/`update_X`/`delete_X`/`list_X` pattern was
repeated identically there on a dozen objects, with the SAME parameters each
time. This is the nominal case of the amendment.

- `attio_record` (18 → 1) — every object shares the SAME resource on the
  client side (`AttioResource`) and therefore the same signature: the object becomes a
  parameter (`object=`), like `module=` at zoho — any slug or id of the workspace,
  custom objects included (be#1158).
- `attio_note` (4 → 1) · `attio_task` (5 → 1) · `attio_list` (5 → 1)
  · `attio_entry` (5 → 1) · `attio_workspace_member` (2 → 1)
  · `attio_meeting` (5 → 1, meeting + its recordings + the transcript: everything
  is keyed by `meeting_id`) · `attio_object` (3 → 1) · `attio_attribute` (4 → 1).
- `attio_comment` (5 → 1) merges comments AND threads: a thread is just the
  string of a comment conversation, both are designated by the SAME
  anchor tuple (`parent_object`+`parent_record_id`, or `list_id`+`entry_id`,
  or `thread_id`) — deleting a head comment deletes the
  thread anyway. Overlapping params ⟹ a single object.

**What was NOT merged**, and why (the criterion is parameter homogeneity,
not the count):
- `attio_entry` stays separate from `attio_list`. The two share `list_id_or_slug`
  and nothing else: list ops work on the CONTAINER (`name`,
  `api_slug`, `workspace_access`), entry ops on its CONTENT (`entry_id`,
  `entry_values`, `parent_record_id`, `filter`, `sorts`,
  `overwrite_multiselect`). Merging would produce a `oneOf` of disjoint
  variants — the schema weight of both tools, plus the ambiguity.
- `attio_note` and `attio_task` stay separate: they only share `content`.
  A note is anchored by `parent_object`/`parent_record_id` + `title`, a task
  by `linked_object`/`linked_record_id` + `deadline`/`assignee_id`, and the API does not
  allow the same verbs (no note update).
- `attio_object` (object definitions) and `attio_attribute` (schema of an object
  OR a list) stay separate: their identifiers do not overlap
  (`object_id_or_slug` vs the `target`+`identifier` pair, where `target` is
  "objects" OR "lists"). Conflating them would make a single `identifier` carry two
  meanings depending on the op.

**What each listing can do, and what it never will** (recorded on
27/08/2026 by differential against the real API, see
`tests/test_attio_listing_window.py` here and in oto-core). Four usage
signals — including #586 and #597, re-reported eleven days later — came from a
daily procedure that did NOT reach the day's call reports:

| object     | pagination                    | sort                  | date window              |
|------------|-------------------------------|-----------------------|--------------------------|
| `note`     | `limit` (def. 10, max 50) + `offset` | **none**       | **none**                 |
| `task`     | `limit` (def. 500, max 1000) + `offset` | `sort`      | **none**                 |
| `meeting`  | `limit` (def. 50, max 200) + `cursor` | `sort`          | `ends_from`/`starts_before` |

⚠️ Attio **SWALLOWS** query parameters it does not know, returning 200
(`/v2/notes` accepts `sort=`, `created_at[gte]` and even `zzz_unknown=x` without
filtering anything). Hence the rule held here: we only expose what upstream
REALLY honors, and a sort or date that does not exist is not simulated on the client
side over a truncated page — that would be a lying filter. Two parameters
were in fact already lying: `attio_task(completed=)` went out as `completed=`
whereas Attio expects `is_completed` (fixed in oto-core, the tool name does not
change), and `attio_meeting(offset=)` was ignored by the API (`offset=2000`
returned the same first page) — replaced by `cursor`, and the old argument is
now REFUSED by naming it rather than accepted with no effect.

⚠️ This module WRITES to a REAL CRM (customer data): `op="create"`/`"update"`/
`"delete"` of `attio_record`, `attio_note`, `attio_task`, `attio_list`,
`attio_entry` and `attio_comment` — and its SCHEMA: `op="create"`/`"create_option"`/
`"create_status"` of `attio_attribute` (oto#256), which the API cannot undo
(no DELETE on an attribute, an option or a stage). The default of EVERY tool is a READ
(`op="list"`, `"query"` or `"threads"`) — a call without `op` can neither write nor
delete. An unknown op is refused BEFORE even resolving the key.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Optional, get_args
from urllib.parse import urlsplit

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from oto.tools.common.errors import UpstreamHTTPError

from .. import access
from ..connectors import verify as connector_verify

# Ops of each object, in reads → writes order. Single source: the
# MCP SCHEMA (`Literal` → JSON `enum`: since the consolidation the verb is
# no longer in the tool name, so the enum is what announces it to the client), input
# validation AND the refusal message derive from it — an added op cannot
# be accepted without being announced (nor the reverse).
_RecordOp = Literal["list", "get", "search", "create", "update", "merge", "delete"]
_NoteOp = Literal["list", "get", "create", "delete"]
_TaskOp = Literal["list", "get", "create", "update", "delete"]
_ListOp = Literal["list", "get", "views", "create", "update"]
_EntryOp = Literal["query", "get", "create", "update", "delete"]
_MemberOp = Literal["list", "get"]
_CommentOp = Literal["threads", "thread", "get", "create", "delete"]
_MeetingOp = Literal["list", "get", "recordings", "recording", "transcript"]
# Sorts accepted by upstream — the schema enum avoids a round trip on an opaque
# 400 ("Query params validation error", without saying what was allowed).
# ⚠️ No equivalent on notes: `/v2/notes` has NO sort (it SWALLOWS the
# parameter, returning 200), so nothing to declare there.
_TaskSort = Literal["created_at:asc", "created_at:desc",
                     "completed_at:asc", "completed_at:desc"]
_MeetingSort = Literal["start_asc", "start_desc"]
_ObjectOp = Literal["list", "get", "views"]
_AttributeOp = Literal["list", "get", "options", "statuses",
                       "create", "create_option", "create_status"]

# `object` of `attio_record`: any object of the workspace, standard or custom, by
# slug or id (be#1158). It lands in the URL path (`objects/{object}/records`): the
# form is checked before any credential resolution, so that nothing but a slug or
# an id — no `/`, no `..` — ever reaches the client.
_OBJECT_REF = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RECORD_OPS = get_args(_RecordOp)
_NOTE_OPS = get_args(_NoteOp)
_TASK_OPS = get_args(_TaskOp)
_LIST_OPS = get_args(_ListOp)
_ENTRY_OPS = get_args(_EntryOp)
_MEMBER_OPS = get_args(_MemberOp)
_COMMENT_OPS = get_args(_CommentOp)
_MEETING_OPS = get_args(_MeetingOp)
_OBJECT_OPS = get_args(_ObjectOp)
_ATTRIBUTE_OPS = get_args(_AttributeOp)


def _one_of(name: str, values: tuple[str, ...]) -> str:
    """Refusal message DERIVED from the list of accepted values — never copied by
    hand: an op added to the tuple announces itself."""
    quoted = [f"'{v}'" for v in values]
    return f"{name} must be " + ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Mandatory argument for THIS op — actionable error, never a fallback.

    An EMPTY value counts as absent: `attributes={}` on `op='create'`
    would create an empty record in the CRM, and on `op='update'` a PATCH that
    changes nothing — two writes that would pass for a success when nothing
    was asked.
    """
    if value is None or (isinstance(value, (str, list, dict)) and not value):
        raise _bad(f"op='{op}' requires {name}")
    return value


# --- What Attio said, the agent must read it — bounded (oto#42) ---------------
#
# `AttioClient._request` (oto-core) raises `UpstreamHTTPError` (service `attio`) on any
# non-2xx answer, 429 included: `status_code`, `method`, `url` (without query),
# `retry_after` on a 429, and `body` = Attio's whole JSON envelope (or the raw text cut
# at 2000 c. when it is not JSON). The taxonomy reads the status from it.
#
# Measured on signal #610 (2026-08-28), when the client still raised a bare `Exception`:
# three company creations refused with 400 `uniqueness_conflict`, Attio naming the field
# ("slug "domains"") AND the record already holding the domain, served as "internal
# error"; the agent concluded a bug with the `.co.uk` public suffix and spent four calls
# isolating a cause that did not exist.
#
# What stays ours: the BODY. oto-core's message is "attio HTTP <code>: <whole envelope>",
# and a third-party error body can carry workspace data. So we re-type at the only point
# where the backend touches this client, with the body reduced by whitelist. Not as
# `McpError`: it would overwrite the machine verdict (`code` / `retryable`) that the
# taxonomy derives from the status — curated message and correct verdict are mutually
# exclusive on that path.

# Bounds on what we let through of the upstream body. A third-party error body can
# carry workspace data: we NEVER relay it wholesale.
_BODY_KEEP = ("code", "path", "message")   # and nothing else
_MAX_MESSAGE = 400                         # characters of Attio's sentence
_MAX_OPAQUE = 120                          # characters of a non-JSON body
# ⚠️ DECLARED coupling (and enforced by the test bench) with `error_taxonomy._LONG_ID`, which
# replaces any token of ≥ 20 characters with `[id]`: `unknown_filter_attribute_slug`
# (29 c.) would render as "attio HTTP 400: [id] — …", i.e. an `[id]` at the head of the
# message that reads like a redacted identifier when it is the NAME of the refusal.
# A code that is too long is therefore omitted rather than served unrecognizable — Attio's sentence,
# for its part, always restates it in clear ("Unknown attribute slug: …").
_MAX_CODE = 19
# Where Attio's endpoints start in the URL the error carries (`AttioClient.BASE_URL`).
_API_PREFIX = "/v2/"


def _upstream_body(body) -> str:
    """Attio's error body reduced to what an agent can USE, bounded.

    Attio answers with a JSON envelope `{status_code, type, code, message, path?}`. We
    relay only `_BODY_KEEP`: `code` (nature of the refusal), `path` (the faulty field
    when Attio gives it) and `message` (the actionable sentence), truncated to
    `_MAX_MESSAGE`. Everything else is dropped BY CONSTRUCTION — a key that Attio
    would add tomorrow does not get through, it is not in the list.

    ⚠️ What gets through anyway comes from the caller's workspace, reached with the
    caller's credential (the id of the conflicting record, the requested id not found):
    no tenant boundary is crossed. What we bound is the VOLUME and
    the UNKNOWN, not a secret. The taxonomy then runs its `scrub`, which replaces
    any token of ≥ 20 characters with `[id]` — identifiers therefore do not reach
    the agent readable, the field and the reason do.

    Any other body (a front end's error page, "Service Unavailable", JSON that is not an
    object): we only return a one-line snippet of `_MAX_OPAQUE` characters — enough to
    see that upstream did not answer as an API, never an entire document.
    """
    if not isinstance(body, dict):
        brut = body if isinstance(body, str) else json.dumps(body, default=str)
        amorce = " ".join(brut.split())[:_MAX_OPAQUE]
        return f"non-JSON body: {amorce}" if amorce else "empty error body"

    # The whitelist SELECTS: what is not in it no longer exists after this
    # line, without any branch below having to remember it.
    retenu = {cle: body.get(cle) for cle in _BODY_KEEP}

    bouts: list[str] = []
    code = retenu["code"]
    if isinstance(code, str) and 0 < len(code.strip()) <= _MAX_CODE:
        bouts.append(code.strip())
    chemin = retenu["path"]
    if isinstance(chemin, list) and chemin:
        bouts.append("field " + ", ".join(str(p)[:60] for p in chemin[:5]))
    message = retenu["message"]
    if isinstance(message, str) and message.strip():
        phrase = " ".join(message.split())
        if len(phrase) > _MAX_MESSAGE:
            phrase = phrase[:_MAX_MESSAGE].rstrip() + "… (truncated)"
        bouts.append(phrase)
    return " — ".join(bouts) or "error body with no usable field"


def _endpoint(url: Optional[str]) -> str:
    """The Attio endpoint of the refused request, as the agent named it through the tool
    (`objects/companies/records`), read from the URL the error carries."""
    chemin = urlsplit(url or "").path
    return chemin.partition(_API_PREFIX)[2] if _API_PREFIX in chemin else chemin.lstrip("/")


def _as_upstream(exc: Exception) -> Optional[UpstreamHTTPError]:
    """The same Attio refusal with its body reduced (`_upstream_body`), else `None`.

    Status, method, URL and `retry_after` are carried over unchanged. Anything that is not
    an `UpstreamHTTPError` of the `attio` service (a bug of ours, a refusal before any
    call) gives `None` and bubbles up unchanged: we never fabricate a status.
    """
    if not isinstance(exc, UpstreamHTTPError) or exc.service != "attio":
        return None
    if exc.status_code == 429:
        # Attio's 429 body says nothing more than the status; `retry_after` says how long.
        corps = "Attio rate-limited the request."
    else:
        corps = f"{_upstream_body(exc.body)} (on {exc.method} /{_endpoint(exc.url)})"
    return UpstreamHTTPError(exc.status_code, corps, service="attio", method=exc.method,
                             url=exc.url, retry_after=exc.retry_after)


def _rendre_le_refus_lisible(client):
    """Install the re-typing on the client's ONLY HTTP exit, and return the client.

    All of `AttioClient`'s resources call `self.client._request(...)`:
    wrapping this method on the INSTANCE covers the ten tools and all their ops
    in one gesture — instead of fifty-odd call sites of which a single forgotten one
    would stay silent exactly as today.

    If oto-core renamed `_request`, we return the client as is: its refusals then keep
    oto-core's whole envelope, never an outage of all Attio calls. The test bench, for
    its part, requires the method to exist — drift is loud where we read it.
    """
    interne = getattr(client, "_request", None)
    if not callable(interne):
        return client

    def _request(*args, **kwargs):
        try:
            return interne(*args, **kwargs)
        except Exception as exc:
            retype = _as_upstream(exc)
            if retype is None:
                raise
            raise retype from exc

    client._request = _request
    return client


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe — otomata-tech/oto#69. Covers `auth` + RIGHTS.

    `GET /v2/self` — the ONLY Attio endpoint that does not follow the `{"data":
    ...}` shape of the rest of the API: an RFC 7662 introspection. What the docs
    establish, quoted:

    - **authenticated** — Bearer token, like the rest of the API;
    - **no side effect** — "Identify the current access token, the
      workspace it is linked to, and any permissions it has";
    - **the cost** — no mention of credit or of a particular rate limit
      for this call. Absence of a counter, a strong hint, not proof.

    **Authenticated ≠ usable** (class named on oto#69, with pennylane) — TWO
    forms of the same hollow verdict, both explicitly checked:

    ⚠️ A dead key does NOT RAISE here: Attio answers 200 with `{"active": false}`
    (introspection contract), never a 401 — letting it pass silently
    would say "connected" for a revoked token.

    Goes further than authentication, same reason as the Pennylane probe:
    `scope` is a space-separated STRING of permissions, and Attio scopes its API
    object by object (`companies:read`, `people:write`…) — an active token but
    WITH NO scope authenticates and can do nothing, the hollow verdict that
    this probe exists to prevent.

    Does NOT read a quota: Attio has no consumable balance by default
    (`access._QUOTA_DEFAULTS` does not list it).
    """
    from oto.tools.attio.client import AttioClient

    infos = AttioClient(api_key=fields["key"])._request("GET", "self") or {}
    if not infos.get("active"):
        raise RuntimeError(
            "Attio says this token is INACTIVE (revoked or expired) — reconnect "
            f"from Attio. Response: {str(infos)[:200]}")
    if not (infos.get("scope") or "").strip():
        raise RuntimeError(
            "The key does authenticate (workspace \""
            f"{infos.get('workspace_name') or '?'}\" recognized) but carries NO "
            "scope: it will be able to neither read nor write anything. Regenerate it "
            "at Attio ticking the desired permissions.")


def register(mcp: FastMCP) -> None:
    from oto.tools.attio.client import AttioClient, AttioResource

    connector_verify.register("attio", _verify)

    def _client() -> tuple[AttioClient, bool]:
        key, is_platform = access.resolve_api_key("attio")
        # `_rendre_le_refus_lisible`: what Attio answers on error must reach
        # the agent, bounded (oto#42) — otherwise a 400 that NAMES the faulty field renders as
        # "Erreur interne du serveur.".
        return _rendre_le_refus_lisible(AttioClient(api_key=key)), is_platform

    # --- records : any object of the workspace ---------------------------

    @mcp.tool()
    def attio_record(
        object: str,
        op: _RecordOp = "list",
        record_id: Optional[str] = None,
        secondary_record_id: Optional[str] = None,
        attributes: Optional[dict] = None,
        overwrite_multiselect: bool = False,
        query: Optional[str] = None,
        filter: Optional[dict] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """A CRM record — company, person, deal or a record of a custom object:
        list, read, search, create, update, merge, delete.

        `object` picks the Attio object the record lives in, by slug or id:
        "companies", "people", "deals", or any custom object of the workspace.
        The objects that exist are listed by `attio_object(op="list")` — read it
        before writing to a custom object, never guess its slug.

        `op`:
        - **"list"** (default): list records of that object in the Attio CRM
          workspace. Paginated (`limit` / `offset`).
        - **"get"**: fetch a record by its Attio record ID.
        - **"search"**: records whose NAME contains `query` (case-insensitive
          substring — an email address or a domain does NOT match; on a custom
          object without a `name` attribute, use `filter`), or records
          matching `filter`, an Attio filter object: `{"email_addresses":
          "ada@acme.com"}` finds the person carrying that address — the check to
          run before creating a person. One of the two, never both.
        - **"create"** — ⚠️ WRITES: create a record from `attributes`.
        - **"update"** — ⚠️ WRITES: update a record. PATCH by default: a
          multiselect value you pass is ADDED to the existing ones, and an empty
          list changes nothing. `overwrite_multiselect=true` REPLACES them
          instead (`[]` empties the field) — the way to free a unique value, a
          domain or an address, before putting it on another record.
        - **"merge"** — ⚠️ WRITES, IRREVERSIBLE: merge `secondary_record_id`
          into `record_id` (same object). Attio marks BOTH as merged — neither
          can be read afterwards — and creates a THIRD record, returned as
          `new_record_id`; where both hold a value, the primary's wins. Not
          idempotent (a replay answers 404). Beta at Attio.
        - **"delete"** — ⚠️ WRITES: delete a record by ID. Irreversible.

        Args:
            object: slug or id of the Attio object — companies | people | deals
                | a custom object (`attio_object(op="list")` lists them).
            op: list (default) | get | search | create | update | merge | delete.
            record_id: op="get"/"update"/"delete"/"merge" — Attio record ID (for
                merge, the PRIMARY record — the one whose values win).
            secondary_record_id: op="merge" — the record merged into `record_id`.
            attributes: op="create"/"update" — Attio attribute dict. Keys are the
                slugs of that object in the workspace; each value follows Attio's
                value format (typically a list, e.g.
                `{"name": [{"value": "Acme"}]}`). On update, same slugs →
                value(s), Attio value format.
                - companies: `name`, `domains`, `description`, `categories`, etc.
                - people: `name`, `email_addresses`, `phone_numbers`, `company`,
                  `job_title`, etc.
                - deals: `name` (str or [{"value": ...}]), `stage` (the TITLE of
                  one of the workspace's deal statuses), `owner`
                  (actor-reference — auto-filled with the first workspace member
                  when omitted), `value`, `associated_company` /
                  `associated_people` ([{"target_object": "companies",
                  "target_record_id": ...}]).
                - custom object: its own attributes, nothing assumed.
                Every other slug, and the deal stages, belong to the workspace:
                read them with `attio_attribute(target="objects",
                identifier="<object>", op="list")` and `op="statuses",
                attribute="stage"` — never assume one.
            overwrite_multiselect: op="update" — true = PUT: the multiselect
                values you pass REPLACE the existing ones (`[]` empties them)
                instead of being added.
            query: op="search" — substring of the record NAME.
            filter: op="search" — Attio filter object, `{"<attribute slug>":
                <value>}` or with an operator (`{"$contains": ...}`); instead of
                `query`.
            limit: op="list"/"search" — max records (default 50).
            offset: op="list" — pagination offset.
        """
        # Refuse BEFORE any credential resolution: an unknown op (or object)
        # never reaches the client — hence never, via a derived path,
        # a write to the CRM.
        if op not in _RECORD_OPS:
            raise _bad(_one_of("op", _RECORD_OPS))
        if not isinstance(object, str) or not _OBJECT_REF.fullmatch(object):
            raise _bad("object must be the slug or id of an Attio object "
                       "(letters, digits, '_' or '-') — list them with "
                       "attio_object(op='list')")
        # #880 — the client lets `filters` overwrite `query` silently: both
        # together would only perform one of the two searches.
        if op == "search" and query and filter:
            raise _bad("op='search': query OR filter, not both — the filter "
                       "would replace the name search without saying so")
        client, is_platform = _client()
        resource = AttioResource(client, object)

        if op == "list":
            result = resource.list(limit=limit, offset=offset)
        elif op == "get":
            result = resource.get(_need(record_id, "record_id", op))
        elif op == "search":
            # `query` is only compared to the NAME (oto-core: `name $contains`) — an
            # address or a domain is not found there (#880); the Attio filter, for its part,
            # reaches any attribute.
            if not query and not filter:
                raise _bad("op='search' requires query (substring of the name) or filter "
                           "(Attio filter object, e.g. {\"email_addresses\": \"…\"})")
            result = resource.search(query=query, filters=filter, limit=limit)
        elif op == "create":
            values = dict(_need(attributes, "attributes", op))
            # `owner` is mandatory on the workspace side for a deal: without it
            # creation fails. We fall back on the 1st workspace member rather than
            # failing the agent on a field it cannot guess.
            if object == "deals" and "owner" not in values:
                members = client.workspace_members.list().get("data", [])
                if members:
                    values["owner"] = [{
                        "referenced_actor_type": "workspace-member",
                        "referenced_actor_id": members[0]["id"]["workspace_member_id"],
                    }]
            result = resource.create(**values)
        elif op == "update":
            # #887 — PATCH appends to multiselects; PUT (overwrite) replaces them.
            result = resource.update(_need(record_id, "record_id", op),
                                     overwrite_multiselect=overwrite_multiselect,
                                     **_need(attributes, "attributes", op))
        elif op == "merge":
            # #886 — irreversible: the two original records can no longer be read, Attio
            # creates a third. Both ids are required, never guessed.
            result = resource.merge(_need(record_id, "record_id", op),
                                    _need(secondary_record_id, "secondary_record_id", op))
        elif op == "delete":
            result = resource.delete(_need(record_id, "record_id", op))
        else:
            # Structurally unreachable (input guard above) — safety net
            # against an implicit `return None` if an op were added to the tuple
            # without its branch: better to refuse than to return "nothing" as a
            # success. Same safety net in each tool below.
            raise _bad(_one_of("op", _RECORD_OPS))

        return result

    # --- notes ------------------------------------------------------------

    @mcp.tool()
    def attio_note(
        op: _NoteOp = "list",
        note_id: Optional[str] = None,
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        title: Optional[str] = None,
        content: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict:
        """A note attached to a record — list, read, create, delete.

        `op`:
        - **"list"** (default): list notes — optionally scoped to a parent record
          (`parent_object` + `parent_record_id`).
        - **"get"**: get a single note by ID (including markdown content).
        - **"create"** — ⚠️ WRITES: create a note attached to a record.
        - **"delete"** — ⚠️ WRITES: delete a note by ID. Irreversible. The Attio
          API does not support editing a note body — to change a note, delete it
          and create a new one.

        ⚠️ **op="list" returns OLDEST first, 10 at a time by default, and Attio
        offers no sort and no date filter here** (unknown query params are
        swallowed with a 200, so a date filter would be a lie). Recent notes are
        therefore at the END: page `offset` by 50 until a page is shorter than
        `limit`. There is no total count. When the record is known, scoping on
        `parent_object`+`parent_record_id` is far cheaper.

        Args:
            op: list (default) | get | create | delete.
            note_id: op="get"/"delete" — the note ID.
            parent_object: companies | people | deals. Optional on op="list"
                (scopes the listing), required on op="create".
            parent_record_id: record ID under that object. Optional on op="list",
                required on op="create" (the record to attach the note to).
            title: op="create" — note title.
            content: op="create" — markdown body.
            limit: op="list" — 1-50, Attio's default is 10.
            offset: op="list" — notes to skip; the only way to reach recent ones.
        """
        if op not in _NOTE_OPS:
            raise _bad(_one_of("op", _NOTE_OPS))
        client, is_platform = _client()

        if op == "list":
            result = client.notes.list(
                parent_object=parent_object, parent_record_id=parent_record_id,
                limit=limit, offset=offset,
            )
        elif op == "get":
            result = client.notes.get(_need(note_id, "note_id", op))
        elif op == "create":
            result = client.notes.create(
                parent_object=_need(parent_object, "parent_object", op),
                parent_record_id=_need(parent_record_id, "parent_record_id", op),
                title=_need(title, "title", op),
                content=_need(content, "content", op),
            )
        elif op == "delete":
            result = client.notes.delete(_need(note_id, "note_id", op))
        else:
            raise _bad(_one_of("op", _NOTE_OPS))

        return result

    # --- tasks ------------------------------------------------------------

    @mcp.tool()
    def attio_task(
        op: _TaskOp = "list",
        task_id: Optional[str] = None,
        content: Optional[str] = None,
        deadline: Optional[str] = None,
        completed: Optional[bool] = None,
        is_completed: Optional[bool] = None,
        assignee_id: Optional[str] = None,
        linked_object: Optional[str] = None,
        linked_record_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        sort: Optional[_TaskSort] = None,
    ) -> dict:
        """A task — list, read, create, update, delete.

        `op`:
        - **"list"** (default): list tasks — optionally filtered by completion
          status (`completed`), paged (`limit`/`offset`) and sorted (`sort`).
          Attio's default is OLDEST first (`created_at:asc`) — pass
          `sort="created_at:desc"` to read recent tasks. No date filter exists.
        - **"get"**: get a single task by ID.
        - **"create"** — ⚠️ WRITES: create a task, optionally linked to a record.
        - **"update"** — ⚠️ WRITES: update a task. The Attio API only allows
          changing `deadline_at`, `is_completed`, `assignees`, `linked_records` —
          the task text itself cannot be edited.
        - **"delete"** — ⚠️ WRITES: delete a task by ID. Irreversible.

        Args:
            op: list (default) | get | create | update | delete.
            task_id: op="get"/"update"/"delete" — the Attio task ID.
            content: op="create" — task description (max 2000 chars).
            deadline: op="create"/"update" — ISO datetime or YYYY-MM-DD.
            completed: op="list" — filter on completion status (True/False).
                This one FILTERS; to change a task's status use `is_completed`.
            is_completed: op="update" — mark as done/not done.
            assignee_id: workspace member ID (op="update", and optional on
                op="create" — defaults to the first workspace member).
            linked_object: companies | people (also deals on op="update") — pair
                with `linked_record_id`.
            linked_record_id: record ID under that object.
            limit: op="list" — 1-1000, Attio's default is 500.
            offset: op="list" — tasks to skip.
            sort: op="list" — created_at:asc (Attio's default) | created_at:desc
                | completed_at:asc | completed_at:desc.
        """
        if op not in _TASK_OPS:
            raise _bad(_one_of("op", _TASK_OPS))
        client, is_platform = _client()

        if op == "list":
            result = client.tasks.list(completed=completed, limit=limit,
                                        offset=offset, sort=sort)
        elif op == "get":
            result = client.tasks.get(_need(task_id, "task_id", op))
        elif op == "create":
            result = client.tasks.create(
                content=_need(content, "content", op),
                deadline=deadline,
                assignee_id=assignee_id,
                linked_object=linked_object,
                linked_record_id=linked_record_id,
            )
        elif op == "update":
            result = client.tasks.update(
                _need(task_id, "task_id", op),
                deadline=deadline,
                is_completed=is_completed,
                assignee_id=assignee_id,
                linked_object=linked_object,
                linked_record_id=linked_record_id,
            )
        elif op == "delete":
            result = client.tasks.delete(_need(task_id, "task_id", op))
        else:
            raise _bad(_one_of("op", _TASK_OPS))

        return result

    # --- lists ------------------------------------------------------------

    @mcp.tool()
    def attio_list(
        op: _ListOp = "list",
        list_id_or_slug: Optional[str] = None,
        name: Optional[str] = None,
        parent_object: Optional[str] = None,
        api_slug: Optional[str] = None,
        workspace_access: str = "full-access",
        attributes: Optional[dict] = None,
    ) -> dict:
        """An Attio list (a saved collection of records) — list, read, create,
        update, and read its saved views.

        This is the CONTAINER. What is IN a list (adding/removing/querying
        records) is `attio_entry`.

        `op`:
        - **"list"** (default): list all Attio lists accessible to the token.
        - **"get"**: get a single list by ID or slug.
        - **"views"**: list the saved views of a list.
        - **"create"** — ⚠️ WRITES: create a new list. `api_slug` and
          `workspace_member_access` are required by the Attio API; the client
          derives/defaults them automatically (slug from the name, empty member access).
        - **"update"** — ⚠️ WRITES: update an existing list (name, api_slug,
          access controls), via `attributes`.

        Args:
            op: list (default) | get | views | create | update.
            list_id_or_slug: op="get"/"views"/"update" — the target list.
            name: op="create" — display name.
            parent_object: op="create" — object slug the list targets
                (companies | people | deals | custom).
            api_slug: op="create" — optional API slug (auto-derived if omitted).
            workspace_access: op="create" — full-access | read-and-write |
                read-only.
            attributes: op="update" — fields to change (name, api_slug, access
                controls).
        """
        if op not in _LIST_OPS:
            raise _bad(_one_of("op", _LIST_OPS))
        client, is_platform = _client()

        if op == "list":
            result = client.lists.list()
        elif op == "get":
            result = client.lists.get(_need(list_id_or_slug, "list_id_or_slug", op))
        elif op == "views":
            result = client.lists.views(_need(list_id_or_slug, "list_id_or_slug", op))
        elif op == "create":
            result = client.lists.create(
                name=_need(name, "name", op),
                parent_object=_need(parent_object, "parent_object", op),
                api_slug=api_slug,
                workspace_access=workspace_access,
            )
        elif op == "update":
            result = client.lists.update(
                _need(list_id_or_slug, "list_id_or_slug", op),
                **_need(attributes, "attributes", op),
            )
        else:
            raise _bad(_one_of("op", _LIST_OPS))

        return result

    # --- entries (list membership) ----------------------------------------

    @mcp.tool()
    def attio_entry(
        list_id_or_slug: str,
        op: _EntryOp = "query",
        entry_id: Optional[str] = None,
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        entry_values: Optional[dict] = None,
        filter: Optional[dict] = None,
        sorts: Optional[list] = None,
        overwrite_multiselect: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """An entry in a list — i.e. a record's membership of that list: query,
        read, add, update, remove.

        `op`:
        - **"query"** (default): query entries in the list, with optional
          filter/sort.
        - **"get"**: get a single list entry by ID.
        - **"create"** — ⚠️ WRITES: add a record to the list as a new entry.
        - **"update"** — ⚠️ WRITES: update list entry values. PATCH appends
          multiselect by default; pass `overwrite_multiselect=True` for PUT.
        - **"delete"** — ⚠️ WRITES: remove a record from the list by deleting its
          entry. Irreversible.

        Args:
            list_id_or_slug: the target list (required for every op).
            op: query (default) | get | create | update | delete.
            entry_id: op="get"/"update"/"delete" — the entry ID.
            parent_object: op="create" — companies | people | deals | custom slug.
            parent_record_id: op="create" — ID of the record (company/person/deal)
                to add.
            entry_values: op="create" — optional list-specific attribute values ;
                op="update" — the values to write.
            filter: op="query" — Attio filter object (e.g. `{"name": "Acme"}`).
            sorts: op="query" — list of sort dicts.
            overwrite_multiselect: op="update" — True switches PATCH to PUT
                (overwrites multiselect instead of appending).
            limit: op="query" — max entries (default 50).
            offset: op="query" — pagination offset.
        """
        if op not in _ENTRY_OPS:
            raise _bad(_one_of("op", _ENTRY_OPS))
        client, is_platform = _client()

        if op == "query":
            result = client.entries.query(
                list_id_or_slug, filter=filter, sorts=sorts,
                limit=limit, offset=offset,
            )
        elif op == "get":
            result = client.entries.get(list_id_or_slug,
                                        _need(entry_id, "entry_id", op))
        elif op == "create":
            result = client.entries.create(
                list_id_or_slug,
                parent_record_id=_need(parent_record_id, "parent_record_id", op),
                parent_object=_need(parent_object, "parent_object", op),
                entry_values=entry_values,
            )
        elif op == "update":
            result = client.entries.update(
                list_id_or_slug,
                _need(entry_id, "entry_id", op),
                entry_values=_need(entry_values, "entry_values", op),
                overwrite_multiselect=overwrite_multiselect,
            )
        elif op == "delete":
            result = client.entries.delete(list_id_or_slug,
                                           _need(entry_id, "entry_id", op))
        else:
            raise _bad(_one_of("op", _ENTRY_OPS))

        return result

    # --- workspace members ------------------------------------------------

    @mcp.tool()
    def attio_workspace_member(
        op: _MemberOp = "list",
        workspace_member_id: Optional[str] = None,
    ) -> dict:
        """A workspace member (a human with access to the Attio workspace) —
        list, read.

        Their IDs are what `attio_comment` wants as `author_id` and `attio_task`
        as `assignee_id`.

        `op`:
        - **"list"** (default): list all workspace members.
        - **"get"**: get a single workspace member by ID.

        Args:
            op: list (default) | get.
            workspace_member_id: op="get" — the member ID.
        """
        if op not in _MEMBER_OPS:
            raise _bad(_one_of("op", _MEMBER_OPS))
        client, is_platform = _client()

        if op == "list":
            result = client.workspace_members.list()
        elif op == "get":
            result = client.workspace_members.get(
                _need(workspace_member_id, "workspace_member_id", op))
        else:
            raise _bad(_one_of("op", _MEMBER_OPS))

        return result

    # --- comments & threads -----------------------------------------------

    @mcp.tool()
    def attio_comment(
        op: _CommentOp = "threads",
        thread_id: Optional[str] = None,
        comment_id: Optional[str] = None,
        content: Optional[str] = None,
        author_id: Optional[str] = None,
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        list_id: Optional[str] = None,
        entry_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """A comment and the thread it lives in — browse threads, read a thread,
        read/post/delete a comment.

        `op`:
        - **"threads"** (default): list comment threads, filtered by parent record
          or list entry. ⚠️ The Attio API returns **400 without a filter** —
          always scope the listing by `parent_object` + `parent_record_id` (or by
          `list_id` + `entry_id`); an unfiltered call is refused here rather than
          upstream.
        - **"thread"**: get a thread with all its comments (`thread_id`).
        - **"get"**: get a single comment by ID.
        - **"create"** — ⚠️ WRITES: create a comment — either replying in a thread
          or starting one on a record/entry. Provide one of: `thread_id`,
          (`parent_object` + `parent_record_id`), or (`list_id` + `entry_id`).
        - **"delete"** — ⚠️ WRITES: delete a comment. If it heads a thread, the
          whole thread is deleted.

        Args:
            op: threads (default) | thread | get | create | delete.
            thread_id: op="thread" — the thread to read; op="create" — reply in
                that thread.
            comment_id: op="get"/"delete" — the comment ID.
            content: op="create" — the comment body.
            author_id: op="create" — a workspace_member_id (see
                `attio_workspace_member`).
            parent_object: companies | people | deals — anchor of the thread
                (op="threads" filter, op="create" target).
            parent_record_id: record ID under that object.
            list_id: anchor on a list entry, paired with `entry_id`.
            entry_id: list entry ID, paired with `list_id`.
            limit: op="threads" — max threads (default 50).
            offset: op="threads" — pagination offset.
        """
        if op not in _COMMENT_OPS:
            raise _bad(_one_of("op", _COMMENT_OPS))
        client, is_platform = _client()

        if op == "threads":
            # Empirical gotcha: `GET /threads` without a filter answers 400. We say so
            # here, actionable, rather than letting the opaque error bubble up.
            if not (parent_object or parent_record_id or list_id or entry_id):
                raise _bad(
                    "op='threads' requires a parent filter: parent_object + "
                    "parent_record_id (or list_id + entry_id) — the Attio API "
                    "answers 400 on an unfiltered thread list.")
            result = client.threads.list(
                parent_object=parent_object,
                parent_record_id=parent_record_id,
                list_id=list_id,
                entry_id=entry_id,
                limit=limit,
                offset=offset,
            )
        elif op == "thread":
            result = client.threads.get(_need(thread_id, "thread_id", op))
        elif op == "get":
            result = client.comments.get(_need(comment_id, "comment_id", op))
        elif op == "create":
            result = client.comments.create(
                content=_need(content, "content", op),
                author_id=_need(author_id, "author_id", op),
                thread_id=thread_id,
                parent_object=parent_object,
                parent_record_id=parent_record_id,
                list_id=list_id,
                entry_id=entry_id,
            )
        elif op == "delete":
            result = client.comments.delete(_need(comment_id, "comment_id", op))
        else:
            raise _bad(_one_of("op", _COMMENT_OPS))

        return result

    # --- meetings / call recordings / transcripts -------------------------

    @mcp.tool()
    def attio_meeting(
        op: _MeetingOp = "list",
        meeting_id: Optional[str] = None,
        call_recording_id: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        sort: Optional[_MeetingSort] = None,
        ends_from: Optional[str] = None,
        starts_before: Optional[str] = None,
    ) -> dict:
        """A meeting (calendar event synced into Attio) and its call recordings.

        `op`:
        - **"list"** (default): list meetings. The only object in this connector
          Attio can window by date (`ends_from` / `starts_before`) and sort
          (`sort`). Pagination is by CURSOR, not offset: pass the response's
          `pagination.next_cursor` back as `cursor`; a null one means the end.
        - **"get"**: get a single meeting by ID.
        - **"recordings"**: list the call recordings of a meeting.
        - **"recording"**: get a single call recording by ID.
        - **"transcript"**: get the transcript of a call recording, one page of
          speech segments at a time. A long call spans several pages: pass the
          response's `pagination.next_cursor` back as `cursor` for the next one;
          a null one means the end.

        Read-only: Attio does not expose writes on meetings or recordings.

        Args:
            op: list (default) | get | recordings | recording | transcript.
            meeting_id: every op but "list" — the meeting ID.
            call_recording_id: op="recording"/"transcript" — the recording ID.
            limit: op="list" — 1-200, Attio's default is 50.
            cursor: op="list"/"transcript" — previous response's pagination.next_cursor.
            sort: op="list" — start_asc (Attio's default) | start_desc.
            ends_from: op="list" — ISO 8601; meetings ending at/after it.
            starts_before: op="list" — ISO 8601; meetings starting before it.
        """
        if op not in _MEETING_OPS:
            raise _bad(_one_of("op", _MEETING_OPS))
        client, is_platform = _client()

        if op == "list":
            result = client.meetings.list(limit=limit, cursor=cursor, sort=sort,
                                           ends_from=ends_from,
                                           starts_before=starts_before)
        elif op == "get":
            result = client.meetings.get(_need(meeting_id, "meeting_id", op))
        elif op == "recordings":
            result = client.call_recordings.list(_need(meeting_id, "meeting_id", op))
        elif op == "recording":
            result = client.call_recordings.get(
                _need(meeting_id, "meeting_id", op),
                _need(call_recording_id, "call_recording_id", op))
        elif op == "transcript":
            meeting = _need(meeting_id, "meeting_id", op)
            recording = _need(call_recording_id, "call_recording_id", op)
            if not cursor:
                result = client.call_recordings.transcript(meeting, recording)
            else:
                # The pinned client reads the first page only (its method takes no
                # cursor): the next pages go through the SAME `_request`, so their
                # refusals are re-typed like the first one's. Attio pages the
                # segments by `cursor` and answers `pagination.next_cursor`.
                result = client._request(
                    "GET",
                    f"meetings/{meeting}/call_recordings/{recording}/transcript",
                    params={"cursor": cursor})
        else:
            raise _bad(_one_of("op", _MEETING_OPS))

        return result

    # --- meta : objects ---------------------------------------------------

    @mcp.tool()
    def attio_object(
        op: _ObjectOp = "list",
        object_id_or_slug: Optional[str] = None,
    ) -> dict:
        """An object definition in the workspace (system or custom) — list, read,
        and read its saved views.

        `op`:
        - **"list"** (default): list all objects (system + custom) defined in the
          workspace. Useful for an LLM to discover what record types exist beyond
          the standard companies/people/deals (e.g. custom objects like
          "products") — the `object` that `attio_record` then reads and writes.
        - **"get"**: get a single object definition by ID or slug.
        - **"views"**: list the saved views of an object.

        Read-only.

        Args:
            op: list (default) | get | views.
            object_id_or_slug: op="get"/"views" — object ID or slug
                (e.g. "companies").
        """
        if op not in _OBJECT_OPS:
            raise _bad(_one_of("op", _OBJECT_OPS))
        client, is_platform = _client()

        if op == "list":
            result = client.objects.list()
        elif op == "get":
            result = client.objects.get(
                _need(object_id_or_slug, "object_id_or_slug", op))
        elif op == "views":
            result = client.objects.views(
                _need(object_id_or_slug, "object_id_or_slug", op))
        else:
            raise _bad(_one_of("op", _OBJECT_OPS))

        return result

    # --- meta : attributes ------------------------------------------------

    @mcp.tool()
    def attio_attribute(
        target: str,
        identifier: str,
        op: _AttributeOp = "list",
        attribute: Optional[str] = None,
        definition: Optional[dict] = None,
        title: Optional[str] = None,
    ) -> dict:
        """An attribute (the schema) of an object or of a list — read it, and
        add an attribute, a select option or a status (e.g. a deal stage).

        `op`:
        - **"list"** (default): list attributes (schema) on an object or list.
        - **"get"**: get a single attribute definition.
        - **"options"**: list the select options for a select-type attribute.
        - **"statuses"**: list the statuses for a status-type attribute.
        - **"create"** — ⚠️ WRITES THE SCHEMA: add an attribute from `definition`.
        - **"create_option"** — ⚠️ WRITES THE SCHEMA: add the option `title` to
          the select attribute `attribute`.
        - **"create_status"** — ⚠️ WRITES THE SCHEMA: add the status `title` to
          the status attribute `attribute` (`attribute="stage"` on deals: a new
          deal stage).

        Attio's API has no delete for an attribute, an option or a status:
        read the schema first, and create only what is missing.
        The key needs the `object_configuration:read-write` scope
        (`list_configuration:read-write` for a list).

        Args:
            target: "objects" or "lists" — which family `identifier` belongs to.
            identifier: object/list ID or slug (e.g. "companies").
            op: list (default) | get | options | statuses | create |
                create_option | create_status.
            attribute: op="get"/"options"/"statuses"/"create_option"/
                "create_status" — attribute ID or slug.
            definition: op="create" — Attio's attribute object, sent as is:
                `title`, `description`, `api_slug`, `type` (text, number,
                select, status, date, record-reference…), `is_required`,
                `is_unique`, `is_multiselect`, `config` (`{}` for most types).
            title: op="create_option"/"create_status" — the option or status
                label.
        """
        if op not in _ATTRIBUTE_OPS:
            raise _bad(_one_of("op", _ATTRIBUTE_OPS))
        client, is_platform = _client()

        if op == "list":
            result = client.attributes.list(target, identifier)
        elif op == "get":
            result = client.attributes.get(target, identifier,
                                           _need(attribute, "attribute", op))
        elif op == "options":
            result = client.attributes.options(target, identifier,
                                               _need(attribute, "attribute", op))
        elif op == "statuses":
            result = client.attributes.statuses(target, identifier,
                                                _need(attribute, "attribute", op))
        elif op == "create":
            result = client.attributes.create(target, identifier,
                                              _need(definition, "definition", op))
        elif op == "create_option":
            result = client.attributes.create_option(
                target, identifier, _need(attribute, "attribute", op),
                _need(title, "title", op))
        elif op == "create_status":
            result = client.attributes.create_status(
                target, identifier, _need(attribute, "attribute", op),
                _need(title, "title", op))
        else:
            raise _bad(_one_of("op", _ATTRIBUTE_OPS))

        return result
