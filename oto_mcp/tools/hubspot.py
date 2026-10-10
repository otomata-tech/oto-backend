"""HubSpot CRM — contacts, companies, deals, tickets, notes (read + write).

Wraps `oto.tools.hubspot.HubSpotClient` (private app token). Key resolved per
call via `access.resolve_api_key("hubspot")` — byo (user key on /account or
the org's shared credential). No platform key.

Generic surface: `object_type` = contacts | companies | deals | tickets
(or any custom object) for search/get/create/update/delete — lossless merge.

**Consolidated surface (ADR 0047 §Amendment)**: 9 tools → 2. The EIGHT verbs that
carried `object_type` (`search`/`get`/`list`/`create`/`update`/`delete`/
`associations`/`create_note`) live in **`hubspot_object`**, the verb in `op` —
they already shared their parameters (`object_type`, `object_id`, `properties`),
and THAT is the merge criterion, not the count. **`hubspot_owners` stays ALONE**:
it takes no CRM object parameter (no `object_type`, no `object_id`, no
`properties`) and reads a user reference list, not a record — merging it
would not have factored out any parameter, so it would have weighed as much as two tools.

⚠️ Two parameters are HOMONYMS whose type depends on the `op` — that is the price
of the merge, and it is paid by HARD validation (never a coercion or a silent
fallback: the wrong shape raises here rather than going to HubSpot,
which would answer with an opaque 400):
- `properties` = list[str] (names of properties to RETURN) on reads
  (search/list/get); dict {property: value} to WRITE on writes
  (create/update).
- `associations` = list[str] (object types whose linked ids we want) on
  op="get"; list[dict] (HubSpot v3 association objects) on op="create".

**`hubspot_list` — the "segments"**. HubSpot lists ARE the segmentation
mechanism (the docs describe them as serving "record segmentation"), there is no
separate `segments` API. Two structural traps, handled here and not by
the agent:
1. Lists are keyed on a NUMERIC `objectTypeId` (`0-1` contacts, `0-2`
   companies, `0-3` deals, `0-5` tickets, `2-<n>` custom) whereas the rest of the
   connector speaks in `"contacts"`. We accept the name AND the raw id, and translate.
2. A `DYNAMIC` list REFUSES membership writes (its members are
   recomputed from its criteria). We read its `processingType` BEFORE writing
   to return an actionable message, rather than letting an opaque 400 go out.

**`filterBranch` is a deliberate pass-through**: HubSpot's criteria tree is
recursive (`filterBranchType` OR/AND/UNIFIED_EVENTS/ASSOCIATION, `operation` shape
per `filterType`) — modelling it would cost a page of schema for little gain. We
pass it as is, as a dict, documented as advanced. It is `hubspot_property` that
makes this pass-through usable: a `filterBranch` references properties by INTERNAL
NAME (`dealstage`, not "Deal stage") and dropdowns only accept
their `options[].value` — without this reference, every criterion (and every create/update)
is guesswork.

3. A membership carries ONLY `recordId`. Reading seven columns per member
   therefore cost one `hubspot_object op='get'` PER member — an N+1 that, at four
   calls per lead, hits the private app ceiling (190 requests / 10 s) around
   the fortieth record, and an uncaught 429 stops the run in the middle of a
   half-written record. `op='members'` therefore accepts `properties`: it
   composes the memberships page with ONE batch read (`batch_read_objects`,
   sliced to 100 by the client) and returns complete rows. Without `properties`,
   the op answers exactly what it has always answered, in a single call.

⚠️ **Scopes**: lists require `crm.lists.read` / `crm.lists.write` in the
private app. Tokens created before these tools only have the `crm.objects.*` scopes
→ a 403 here means "add the scope", NOT "invalid key".
"""
from __future__ import annotations

import re
from typing import Literal, Optional, Union

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from .hubspot_pipelines import (
    add_labels, compact_property, fill_pipeline_options, filter_properties)
from .hubspot_scopes import measure_scopes, missing_scopes_refusal, translate


#: The keys that `batch_read_objects` ALWAYS returns, both of them, never
#: None (oto-core contract). Written once, here: it is the only description
#: of the client's shape in this repo, and the refusal below rests on it.
_CLES_ENVELOPPE = ("results", "missing_ids")


def _batch_read_envelope(lecture) -> tuple:
    """Open the ENVELOPE of `batch_read_objects` — a mapping, not a list.

    oto-core returns `{"results": [...], "missing_ids": [...]}`. `missing_ids` is
    the tally of requested ids that HubSpot did not return: its batch read
    answers 207 without naming the absentees, and the client is the ONLY place where this
    gap is computed. We hand it back at the call site rather than letting it
    drop — a page of 250 members that comes back as 247 rows must announce itself.

    ⚠️ The refusal is the reason this function exists, and it targets BOTH
    keys BY NAME, not just the type of the container:

    - a client that returns a bare LIST (the pre-tag shape): taking it
      for the envelope would iterate its KEYS (`"results"`, a string) and raise
      an opaque `AttributeError` deep in the stitching, several frames
      away;
    - a client that RENAMES `missing_ids`: that is the silent accident, and the
      worse of the two. Reading the key with a default (`.get(…) or []`) would return
      an amputated envelope WITHOUT a word — the page of 250 members that came back as 247
      would announce itself free of any gap, which is exactly the disguised
      success this tally exists to forbid. We therefore require both keys
      by name: a cross-repo drift becomes a refusal, never a page
      silently shrunk.

    It is here, and nowhere else, that the SHAPE of the client is written:
    `tests/test_tools_client_methods_exist.py` proves that the METHOD exists on
    the pinned tag, nothing mechanically proves what it returns.

    Returns `(results, missing_ids)`. The shape of `results` is not judged here:
    it is `_rows_from_memberships` that refuses it, in turn and by name.
    """
    if not isinstance(lecture, dict):
        raise TypeError(
            "batch_read_objects must return the envelope "
            "{'results': [...], 'missing_ids': [...]} and not "
            f"{type(lecture).__name__} — oto-core pin is behind the tag that "
            "carries this shape (see pyproject.toml)")
    defaillantes = [k for k in _CLES_ENVELOPPE if lecture.get(k) is None]
    if defaillantes:
        raise TypeError(
            "batch_read_objects must return the envelope "
            "{'results': [...], 'missing_ids': [...]}: "
            f"missing or null key(s) {defaillantes}, received keys "
            f"{sorted(lecture)} — oto-core pin is behind, or key renamed on the "
            "client side (see pyproject.toml). Serving the page without `missing_ids` would "
            "shrink it without saying so.")
    return lecture["results"], list(lecture["missing_ids"])


def _missing_report(rows, missing_ids) -> dict:
    """The gap keys to serve — TWO verdicts on the same fact, never merged.

    `missing_ids` is the CLIENT's verdict (the ids it requested and that
    HubSpot did not return); `missing_count` is the JOIN's (the rows
    served without `properties`). They must coincide. We serve both
    rather than just one, and we NAME their disagreement instead of silently picking
    one: the day they diverge, one of the two is wrong, and that is
    precisely the kind of gap we refuse to let through without a word.

    ⚠️ **The disagreement is SYMMETRIC, and it became so because the other direction
    happened.** A first version only compared the two sets if
    the client itself had something to say (`if missing_ids and …`): an
    absence seen by the JOIN alone — a membership without `recordId`, hence
    an id never requested from the batch read, which by construction cannot appear
    in `missing_ids` — then served `missing_count: 1` alone, without an id
    or a sentence to say who we are talking about. A number without a name is the worst of
    both worlds: visible enough to worry, too mute to act on. The
    comparison therefore covers both sets, in both directions, as soon as
    either of them is non-empty.

    Pure, and therefore directly exercisable.
    """
    absents_du_join = [str(r.get("recordId")) for r in rows if "missing" in r]
    reportes = [str(i) for i in (missing_ids or [])]
    out: dict = {}
    if reportes:
        out["missing_ids"] = list(missing_ids)
    if absents_du_join:
        out["missing_count"] = len(absents_du_join)
    if set(reportes) != set(absents_du_join):
        out["missing_mismatch"] = {
            "reported_by_client": list(missing_ids or []),
            "absent_from_join": absents_du_join,
            "note": ("the batch read's own verdict and the membership join "
                     "disagree on which records are missing"),
        }
    return out


def _rows_from_memberships(memberships, records, properties=None) -> list[dict]:
    """Stitch a page of memberships to their records, in the page's ORDER.

    A membership only carries `recordId`; the columns come from a separate batch
    read, which can return FEWER (record deleted between the two
    calls, or outside this key's rights). The gap is NAMED (`properties:
    None` + `missing`) rather than filled in or hushed: a mute row in the middle of a
    prospecting population is exactly what we do not want to fabricate.

    We iterate the MEMBERSHIPS, never the records — that is what keeps
    the page order and forbids losing a member along the way. Each row
    starts from the membership AS IS (`recordId` is not renamed): a
    procedure that already reads `results[].recordId` keeps working the day
    it starts passing `properties`.

    Pure (no client, no context, no closure) and therefore at module level, unlike
    the `register()` helpers: the shape of the rows is what we
    want to be able to exercise directly.

    `records` is the `results` LIST of the batch read, not the value returned by
    `batch_read_objects` (which is an envelope): opening it is the job of
    `_batch_read_envelope`, at the call site that knows the client's contract.
    Any other shape is REFUSED here rather than iterated — a dict iterates over
    its keys and would give `AttributeError: 'str' object has no attribute 'get'`,
    an opaque failure where a name is needed.

    The ADDED keys are in English, like the rest of this tool's served surface;
    the raised refusals are in English too, like their `_bad` neighbours.
    """
    if records is None:
        records = []
    if not isinstance(records, list):
        raise TypeError(
            "_rows_from_memberships expects the `results` LIST of the batch read, "
            f"not {type(records).__name__}: `batch_read_objects` returns "
            "the envelope {'results': [...], 'missing_ids': [...]}, opened at the "
            "call site by _batch_read_envelope")
    by_id = {str(r.get("id")): r for r in records}
    rows: list[dict] = []
    for m in memberships or []:
        row = dict(m)  # the HubSpot membership VERBATIM (recordId, timestamp, …)
        rec = by_id.get(str(m.get("recordId")))
        row["properties"] = rec.get("properties") if rec else None
        if rec is None:
            row["missing"] = ("record not returned by the batch read "
                              "(deleted, or outside this key's scope)")
        elif properties:
            absent = [p for p in properties
                      if p not in (rec.get("properties") or {})]
            if absent:
                # Separates "HubSpot has no value" from "this internal name
                # does not exist": without it, a misspelled name reads as
                # an empty column — and internal names are exactly
                # what `hubspot_property` exists to provide.
                row["missing_properties"] = absent
        rows.append(row)
    return rows


# HubSpot returns a 403 `MISSING_SCOPES` whose message — "The scope needed for this
# API call isn't available for public use" — reads as "this scope is not
# available to you". The raw body went as is to the agent, which had no reason to
# see a checkbox to tick on the customer's side: the same signal was filed AGAIN
# IDENTICALLY two days in a row by the same daily procedure (#636 then #649). So we
# NAME the action — and now the scope (`hubspot_scopes`). Kept under this name:
# `hubspot_lignes` routes its per-row stop on it.
def _scope_refusal(e, object_type) -> Optional[McpError]:
    """The MISSING_SCOPES 403 translated into an actionable refusal, or None if anything else."""
    return missing_scopes_refusal(e, object_type=object_type)


def _verify(fields: dict, config: dict | None = None) -> dict:
    """"Test the connection" probe — otomata-tech/oto#69. Covers `auth+scopes`.

    1. **auth** — `GET /account-info/v3/details`: an account read (`portalId`,
       `accountType`, `timeZone`…), no side effect. A refusal here RAISES: it is
       the connection's verdict.
    2. **scopes** — HubSpot grants its scopes OBJECT BY OBJECT: a token can read
       contacts and not tickets. That is not a dead connector (oto#69, third rule:
       a partial scope is not the connection's verdict), so it never raises — it
       is RETURNED as a measurement, per family (`hubspot_scopes.measure_scopes`):
       read from the token itself when HubSpot answers, else one minimal read per
       family. Bounded, read-only, and `unknown` for what it could not settle.
    """
    from oto.tools.hubspot.client import HubSpotClient

    client = HubSpotClient(api_key=fields["key"])
    infos = client._request("GET", "/account-info/v3/details") or {}
    if not infos.get("portalId"):
        raise RuntimeError(
            "HubSpot answered without identifying an account for this key — "
            f"unexpected response: {str(infos)[:200]}")
    return {"scopes": measure_scopes(client, fields["key"])}


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError

    from oto.tools.hubspot.client import HubSpotClient

    connector_verify.register("hubspot", _verify, couvre=connector_verify.AUTH_SCOPES)

    def _client() -> HubSpotClient:
        key, _ = access.resolve_api_key("hubspot")
        return HubSpotClient(api_key=key)

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _need(value, name: str, op: str):
        """Required argument for THIS op — actionable error, never a fallback."""
        if value is None:
            raise _bad(f"op='{op}' requires {name}")
        return value

    def _names(value, name: str, op: str) -> Optional[list]:
        """READ form of a homonym parameter: a list of NAMES (list[str]).

        `properties` and `associations` change type depending on the op (see the module
        docstring): we refuse the write form here instead of forwarding it.
        """
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _bad(
                f"op='{op}' expects {name} = list of property names (list[str]); "
                "the dict/objects form is that of the write ops")
        return value

    def _payload(value, name: str, op: str) -> dict:
        """WRITE form of `properties`: a dict {property: value}."""
        _need(value, name, op)
        if not isinstance(value, dict):
            raise _bad(
                f"op='{op}' expects {name} = dict {{property: value}}; the list of "
                "names is the form of the read ops")
        return value

    def _assoc_objects(value, op: str) -> Optional[list]:
        """WRITE form of `associations`: HubSpot v3 association objects."""
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise _bad(
                f"op='{op}' expects associations = list of HubSpot v3 association "
                "objects (list[dict]); the list of types is the form of op='get'")
        return value

    @mcp.tool()
    def hubspot_object(
        op: Literal["search", "list", "get", "create", "update", "delete",
                    "associations", "add_note"] = "search",
        object_type: Optional[str] = None,
        object_id: Optional[str] = None,
        properties: Optional[Union[list[str], dict]] = None,
        associations: Optional[Union[list[str], list[dict]]] = None,
        query: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        to_object_type: Optional[str] = None,
        body: Optional[str] = None,
        limit: int = 100,
        after: Optional[str] = None,
        resolve_labels: bool = False,
    ) -> dict:
        """HubSpot CRM objects — one tool, the verb in `op`.

        `object_type` = contacts | companies | deals | tickets (or any custom
        object) drives every op, and is always required.

        Ops:
        - **"search"** (default) : Search CRM objects. Full-text via `query`,
          structured via `filters`. Paginated (`limit`, `after`).
        - **"list"** : List CRM objects of a type (paginated via `after`).
        - **"get"** : Fetch one CRM object by id. Requires `object_id`.
        - **"create"** : Create a CRM object. Requires `properties` (dict).
        - **"update"** : Update (PATCH) a CRM object's properties. Requires
          `object_id` + `properties` (dict).
        - **"delete"** : Archive a CRM object (moves it to HubSpot's recycle bin).
          Requires `object_id`.
        - **"associations"** : List objects of `to_object_type` associated with an
          object. e.g. the deals of a contact: object_type="contacts",
          to_object_type="deals". Requires `object_id` + `to_object_type`.
        - **"add_note"** : Attach a note to a CRM object (contacts/companies/deals/
          tickets). Requires `body` + `object_id` (the object the note hangs on).

        ⚠️ `properties` and `associations` are HOMONYMS whose expected type depends
        on `op` (read = list of names, write = dict / association objects) — see the
        Args below. A wrong shape is refused with an explicit error, never coerced.

        Many contacts/companies: hubspot_push_rows reads them from a datastore table.

        Args:
            op: search | list | get | create | update | delete | associations |
                add_note.
            object_type: contacts | companies | deals | tickets (or custom).
                Required for every op ; on op="add_note" it is the type of the
                object the note is attached to.
            object_id: id of the object — required for get, update, delete,
                associations and add_note.
            properties:
                - READ (search, list, get) : property names to return (list[str]).
                - WRITE (create, update) : object properties (dict), e.g.
                  {"email": …, "firstname": …} for a contact ; {"dealname": …,
                  "amount": …} for a deal.
            associations:
                - op="get" : other object types to return associated ids for
                  (e.g. ["companies", "deals"] on a contact) — this returns the
                  ids INLINE, so it replaces a separate op="associations" round
                  trip per object.
                - op="create" : HubSpot v3 association objects (advanced).
            query: op="search" — full-text search.
            filters: op="search" — list of {propertyName, operator, value} combined
                with AND. Passed to HubSpot VERBATIM — nothing here validates the
                operator, so HubSpot's own set is the reference: EQ, NEQ, LT, LTE,
                GT, GTE, BETWEEN (add "highValue"), IN / NOT_IN (pass "values":
                [...] instead of "value"), HAS_PROPERTY, NOT_HAS_PROPERTY,
                CONTAINS_TOKEN, NOT_CONTAINS_TOKEN (wildcards `*`).
            to_object_type: op="associations" — the associated object type to list.
            body: op="add_note" — the note content (text/HTML).
            limit: op="search"/"list" — page size (HubSpot caps it at 100).
            after: op="search"/"list" — pagination cursor from a previous response
                (paging.next.after).
            resolve_labels: op="search"/"list"/"get" on deals or tickets — add
                `dealstage_label` / `pipeline_label` (`hs_pipeline_stage_label` /
                `hs_pipeline_label` on tickets) NEXT TO the raw ids, which stay
                (writes need them). One pipelines read per call.
        """
        c = _client()

        def _labelled(result):
            # Opt-in, and only ever ADDS sibling keys: without the flag the client's
            # answer goes back untouched, exactly as before.
            if resolve_labels:
                add_labels(result, object_type, c)
            return result

        try:
            if op == "search":
                return _labelled(c.search_objects(
                    _need(object_type, "object_type", op),
                    query=query, filters=filters,
                    properties=_names(properties, "properties", op),
                    limit=limit, after=after))

            if op == "list":
                return _labelled(c.list_objects(
                    _need(object_type, "object_type", op),
                    properties=_names(properties, "properties", op),
                    limit=limit, after=after))

            if op == "get":
                return _labelled(c.get_object(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op),
                    properties=_names(properties, "properties", op),
                    associations=_names(associations, "associations", op)))

            if op == "create":
                return c.create_object(
                    _need(object_type, "object_type", op),
                    _payload(properties, "properties", op),
                    associations=_assoc_objects(associations, op))

            if op == "update":
                return c.update_object(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op),
                    _payload(properties, "properties", op))

            if op == "delete":
                return c.delete_object(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op))

            if op == "associations":
                return c.list_associations(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op),
                    _need(to_object_type, "to_object_type", op))

            if op == "add_note":
                return c.create_note(
                    _need(body, "body", op),
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op))

            raise _bad("op must be 'search', 'list', 'get', 'create', 'update', "
                       "'delete', 'associations' or 'add_note'")
        except UpstreamHTTPError as e:
            refus = translate(e, object_type=object_type, object_id=object_id)
            if refus is None:
                raise          # any other upstream refusal keeps its shape and its trace
            raise refus from None

    # objectTypeId: lists are keyed on the numeric id, not on the object
    # name. We accept both — the name for the four standard ones, the raw `N-N` id
    # for everything else (no table can cover custom objects,
    # whose id depends on the portal).
    _OBJECT_TYPE_IDS = {
        "contacts": "0-1", "companies": "0-2", "deals": "0-3", "tickets": "0-5",
    }

    # The INVERSE of the table above: a list carries its `objectTypeId`, but
    # the batch read endpoint is keyed on the object name.
    _LIST_OBJECT_NAMES = {v: k for k, v in _OBJECT_TYPE_IDS.items()}

    def _object_type_id(value, op: str) -> str:
        """Translate `object_type` into a HubSpot `objectTypeId` for lists."""
        _need(value, "object_type", op)
        key = str(value).strip().lower()
        if key in _OBJECT_TYPE_IDS:
            return _OBJECT_TYPE_IDS[key]
        if re.fullmatch(r"\d+-\d+", key):
            return key  # raw id (custom object: `2-<n>`)
        raise _bad(
            f"object_type='{value}' unknown for lists: expected "
            "contacts | companies | deals | tickets, or the raw objectTypeId "
            "of a custom object (form '2-7', visible in the HubSpot settings)")

    def _ids(value, name: str, op: str) -> list:
        """List of record ids — HubSpot wants them as strings."""
        _need(value, name, op)
        if not isinstance(value, list) or not value:
            raise _bad(f"op='{op}' expects {name} = non-empty list of ids")
        return [str(v) for v in value]

    def _batch_object_type(c, list_id: str, object_type, op: str) -> str:
        """What object type the members are — DERIVED, never guessed.

        A membership only carries `recordId`; the batch read, on the other hand, is keyed
        by object type. Since `op='members'` has always taken `list_id` ALONE, we
        read the type off the list record (one GET, the very one that
        `_writable_list` already does before writing) instead of requiring a new
        argument on an op that already has callers. Passing `object_type`
        explicitly saves that GET.

        A type that cannot be guessed is REFUSED, naming the argument that would give it:
        falling back on "contacts" would read the wrong object and return a
        plausible and wrong population.
        """
        if object_type is not None:
            key = str(object_type).strip().lower()
            _object_type_id(key, op)  # validates the shape, refuses an unknown type
            return key
        fiche = c.get_list(list_id)
        info = fiche.get("list") or fiche
        type_id = info.get("objectTypeId")
        if not type_id:
            raise _bad(
                f"op='{op}' with properties: cannot determine the object type "
                f"of the members of list {list_id} (its record carries "
                "no objectTypeId) — pass object_type (contacts | companies "
                "| deals | tickets, or the raw objectTypeId of a custom object).")
        return _LIST_OBJECT_NAMES.get(str(type_id), str(type_id))

    def _writable_list(c, list_id: str, op: str) -> dict:
        """Load the list and REFUSE to write its members if it is DYNAMIC.

        A dynamic list recomputes its members from its criteria; HubSpot
        answers a generic 400 on the membership endpoints. A prior GET
        costs little and lets us say what to do instead — and also serves
        as the "before" state for dry_runs.
        """
        current = c.get_list(list_id)
        info = current.get("list") or current
        if info.get("processingType") == "DYNAMIC":
            raise _bad(
                f"op='{op}' impossible: list {list_id} "
                f"(\"{info.get('name')}\") is DYNAMIC — its members are "
                "recomputed by HubSpot. Change its criteria "
                "(op='update' with filter_branch), not its members.")
        return info

    @mcp.tool()
    def hubspot_list(
        op: Literal["search", "get", "create", "update", "delete", "restore",
                    "members", "add_members", "remove_members", "clear_members",
                    "copy_from", "record_lists"] = "search",
        list_id: Optional[str] = None,
        object_type: Optional[str] = None,
        name: Optional[str] = None,
        processing_type: Literal["MANUAL", "DYNAMIC", "SNAPSHOT"] = "MANUAL",
        filter_branch: Optional[dict] = None,
        record_ids: Optional[list] = None,
        remove_record_ids: Optional[list] = None,
        record_id: Optional[str] = None,
        source_list_id: Optional[str] = None,
        query: Optional[str] = None,
        include_filters: bool = False,
        limit: int = 100,
        after: Optional[str] = None,
        properties: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """HubSpot lists — the segments of a HubSpot portal. One tool, verb in `op`.

        A HubSpot "segment" IS a list: there is no separate segments API. Three
        kinds, set at creation and NOT changeable afterwards:
        - **MANUAL** : you decide who is in it (the `*_members` ops below).
        - **DYNAMIC** : HubSpot recomputes membership from `filter_branch`. Its
          membership ops are REFUSED — change the criteria instead.
        - **SNAPSHOT** : filtered once at creation, then managed by hand.

        `object_type` takes the usual name (contacts | companies | deals |
        tickets) and is translated to the numeric objectTypeId lists key on. For
        a custom object, pass its raw id (`"2-7"`).

        Ops:
        - **"search"** (default) : find lists by `name` fragment via `query`,
          optionally narrowed by `object_type`.
        - **"get"** : one list, by `list_id` — or by `name` + `object_type`.
          `include_filters=true` also returns its criteria tree.
        - **"create"** : create a list. Requires `name` + `object_type`. For a
          DYNAMIC/SNAPSHOT list, pass `filter_branch`.
        - **"update"** : rename (`name`) and/or replace the criteria
          (`filter_branch`). Requires `list_id`.
        - **"delete"** : delete a list — restorable for 90 days. Requires
          `list_id`. Supports `dry_run`.
        - **"restore"** : restore a deleted list (within those 90 days).
        - **"members"** : the record ids in a list (paginated, `after`). Pass
          `properties` to get FULL ROWS instead of bare ids — one memberships
          page plus ONE batch read, so a 100-record page costs 2 calls instead
          of the 101 it costs to follow up with `hubspot_object op="get"` per
          member. Do that: a HubSpot private app is capped at 190 requests per
          10 seconds, and the per-member loop hits the ceiling around the
          fortieth lead, mid-record.
        - **"add_members"** / **"remove_members"** : add/remove `record_ids`.
          On op="add_members", also passing `remove_record_ids` does both in a
          SINGLE list revision (one recompute instead of two).
        - **"clear_members"** : remove EVERY record from the list (the list
          survives). Supports `dry_run` — use it.
        - **"copy_from"** : copy every member of `source_list_id` into
          `list_id` (HubSpot caps this at 100 000 records).
        - **"record_lists"** : which lists a given record belongs to. Requires
          `object_type` + `record_id`.

        ⚠️ Requires the `crm.lists.read` / `crm.lists.write` scopes on the
        private app. A token created before these scopes existed answers 403 —
        that means "add the scope", not "the key is wrong".

        Args:
            op: search | get | create | update | delete | restore | members |
                add_members | remove_members | clear_members | copy_from |
                record_lists.
            list_id: the list — required for every op except search, create and
                record_lists.
            object_type: contacts | companies | deals | tickets, or a raw
                objectTypeId ("2-7") for a custom object. On op="members" with
                `properties`, it is the type the members are read as — omitted,
                it is derived from the list itself (one extra GET).
            name: op="create" the list name ; op="update" the new name ;
                op="get" look the list up by name (with `object_type`).
            processing_type: op="create" — MANUAL (default) | DYNAMIC | SNAPSHOT.
            filter_branch: op="create"/"update" — HubSpot's criteria tree, passed
                through verbatim. Recursive shape: {"filterBranchType": "OR",
                "filterBranches": [{"filterBranchType": "AND", "filters": [
                {"filterType": "PROPERTY", "property": "<internal name>",
                "operation": {"operationType": "NUMBER", "operator":
                "IS_GREATER_THAN_OR_EQUAL_TO", "value": 12}}]}]}. Property names
                are the INTERNAL ones — get them from `hubspot_property`.
            record_ids: op="add_members"/"remove_members" — the record ids to
                add / to remove.
            remove_record_ids: op="add_members" only — ids to remove in the same
                revision as the ones being added.
            record_id: op="record_lists" — the single record to look up.
            source_list_id: op="copy_from" — the list to copy members from.
            query: op="search" — name fragment.
            include_filters: op="get" — also return the list's criteria tree.
            limit: op="members" — page size.
            after: op="members" — pagination cursor from a previous response.
            properties: op="members" ONLY — the INTERNAL property names to
                return for each member (get them from `hubspot_property`;
                "Deal stage" is not one). Each row then carries the membership
                keys it already had plus `properties`. A member the batch read
                did not return keeps its row, with `properties: null` and a
                `missing` reason — rows are never dropped. A name absent from a
                returned record is listed in that row's `missing_properties`,
                which is how a typo tells itself apart from an empty column.
                The answer also carries `missing_ids` (the batch read's own
                verdict on what it could not fetch) and `missing_count`, when
                either is non-empty. Omitted, the op answers exactly what it
                always did: ids only. An EMPTY list is refused, not treated as
                "omitted" — it would cost two extra calls to return HubSpot's
                default columns, which is nobody's request.
            dry_run: op="delete"/"clear_members"/"remove_members" — validate and
                report what WOULD change (with the list's current state), without
                writing.
        """
        c = _client()
        try:
            # `properties` means nothing anywhere but on op='members': silencing it
            # would be a MUTE divergence — the caller would believe they asked for
            # columns and would read a result that carries none.
            if properties is not None and op != "members":
                raise _bad(
                    f"op='{op}' does not accept properties: column projection "
                    "only exists on op='members' (for objects, "
                    "it is hubspot_object that carries it)")
            wanted = _names(properties, "properties", op)

            # `properties=[]` asks for ZERO columns. Letting it through would take the
            # enriched path: one more `get_list`, then a batch read whose
            # body omits `properties` — to which HubSpot answers its DEFAULT
            # projection. The caller would pay three calls for columns that
            # nobody asked for. The two possible intents already each have
            # their spelling (omit the argument = ids only; fill it = columns);
            # the third is refused, not guessed.
            if wanted is not None and not wanted:
                raise _bad(
                    f"op='{op}' expects properties = NON-EMPTY list of internal "
                    "property names; properties=[] asks for no column — omit "
                    "the argument to get only the record ids")

            if op == "search":
                return c.search_lists(
                    query=query,
                    object_type_id=(_object_type_id(object_type, op)
                                    if object_type else None))

            if op == "get":
                if list_id:
                    return c.get_list(list_id, include_filters=include_filters)
                if name and object_type:
                    return c.get_list_by_name(
                        _object_type_id(object_type, op), name,
                        include_filters=include_filters)
                raise _bad("op='get' requires list_id, or name + object_type")

            if op == "create":
                if processing_type != "MANUAL" and filter_branch is None:
                    raise _bad(
                        f"processing_type='{processing_type}' requires filter_branch "
                        "(a list without criteria would have no members)")
                return c.create_list(
                    _need(name, "name", op),
                    _object_type_id(object_type, op),
                    processing_type=processing_type,
                    filter_branch=filter_branch)

            if op == "update":
                lid = _need(list_id, "list_id", op)
                if name is None and filter_branch is None:
                    raise _bad("op='update' requires name and/or filter_branch")
                out: dict = {}
                if name is not None:
                    out["renamed"] = c.update_list_name(lid, name)
                if filter_branch is not None:
                    out["filters"] = c.update_list_filters(lid, filter_branch)
                return out

            if op == "delete":
                lid = _need(list_id, "list_id", op)
                if dry_run:
                    return {"dry_run": True, "would": "delete", "list_id": lid,
                            "current": c.get_list(lid),
                            "note": "restorable for 90 days via op='restore'"}
                return c.delete_list(lid)

            if op == "restore":
                return c.restore_list(_need(list_id, "list_id", op))

            if op == "members":
                lid = _need(list_id, "list_id", op)
                page = c.get_list_memberships(lid, limit=limit, after=after)
                if wanted is None:
                    # Historical path INTACT: one call, its response returned as
                    # is — not re-wrapped, not augmented with a key.
                    return page
                membres = (page or {}).get("results") or []
                otype = _batch_object_type(c, lid, object_type, op)
                ids = [str(m.get("recordId")) for m in membres
                       if m.get("recordId") is not None]
                # The slicing at 100 is HubSpot's, hence the CLIENT's:
                # a second slicer here would be a mirror that nothing ties together, and
                # that would drift silently. An empty batch read is a 400 at
                # HubSpot — a page without members has nothing to read.
                #
                # `batch_read_objects` returns an ENVELOPE, not a list:
                # `{"results": [...], "missing_ids": [...]}`. We open it, and we SERVE
                # `missing_ids` — it is the client's verdict on the ids that HubSpot
                # did not return, and re-deriving it here while throwing away its own would turn
                # two computations into a single number, with no way to ever compare them.
                lecture = (c.batch_read_objects(otype, ids, properties=wanted)
                           if ids else {"results": [], "missing_ids": []})
                records, absents = _batch_read_envelope(lecture)
                out = dict(page or {})  # `paging` and `total` survive verbatim
                out["results"] = _rows_from_memberships(membres, records, wanted)
                out["object_type"] = otype  # provenance: the type actually read
                out.update(_missing_report(out["results"], absents))
                return out

            if op == "add_members":
                lid = _need(list_id, "list_id", op)
                ids = _ids(record_ids, "record_ids", op)
                _writable_list(c, lid, op)
                if remove_record_ids:
                    return c.add_and_remove_list_memberships(
                        lid, record_ids_to_add=ids,
                        record_ids_to_remove=_ids(
                            remove_record_ids, "remove_record_ids", op))
                return c.add_list_memberships(lid, ids)

            if op == "remove_members":
                lid = _need(list_id, "list_id", op)
                ids = _ids(record_ids, "record_ids", op)
                info = _writable_list(c, lid, op)
                if dry_run:
                    return {"dry_run": True, "would": "remove_members",
                            "list_id": lid, "record_ids": ids, "current": info}
                return c.remove_list_memberships(lid, ids)

            if op == "clear_members":
                lid = _need(list_id, "list_id", op)
                info = _writable_list(c, lid, op)
                if dry_run:
                    return {"dry_run": True, "would": "clear_members",
                            "list_id": lid, "current": info,
                            "note": "removes ALL members; the list survives"}
                return c.delete_all_list_memberships(lid)

            if op == "copy_from":
                lid = _need(list_id, "list_id", op)
                src = _need(source_list_id, "source_list_id", op)
                _writable_list(c, lid, op)
                return c.add_memberships_from_list(lid, src)

            if op == "record_lists":
                return c.get_record_memberships(
                    _object_type_id(object_type, op),
                    _need(record_id, "record_id", op))

            raise _bad("op must be 'search', 'get', 'create', 'update', 'delete', "
                       "'restore', 'members', 'add_members', 'remove_members', "
                       "'clear_members', 'copy_from' or 'record_lists'")
        except UpstreamHTTPError as e:
            refus = translate(e, object_type=object_type, object_id=list_id,
                              family="lists", what="list")
            if refus is None:
                raise
            raise refus from None

    @mcp.tool()
    def hubspot_property(
        op: Literal["list", "get", "create", "update", "delete", "groups"] = "list",
        object_type: Optional[str] = None,
        property_name: Optional[str] = None,
        definition: Optional[dict] = None,
        archived: bool = False,
        dry_run: bool = False,
        names: Optional[list[str]] = None,
        group: Optional[str] = None,
        include_hidden: bool = False,
        custom_only: bool = False,
        verbose: bool = False,
    ) -> dict:
        """HubSpot properties — the field schema of a CRM object type.

        Read this BEFORE writing anything. HubSpot's internal property names are
        not the labels shown in the UI (`dealstage`, not "Deal Stage"), and an
        enumeration property only accepts its declared `options[].value` — so a
        create/update written from the label is a guess that fails or, worse,
        silently writes nothing. List criteria (`hubspot_list`'s `filter_branch`)
        reference the same internal names.

        Deal and ticket stages belong to a pipeline, not to the property: their
        `options` (`dealstage`, `hs_pipeline_stage`) are filled from the
        pipelines, grouped in `pipeline_options` — `hubspot_pipeline` reads them.

        Ops:
        - **"list"** (default) : the properties of `object_type`, one compact row
          each (name, label, type, fieldType, groupName, options) — hidden
          HubSpot-internal ones left out unless `include_hidden`. `verbose=true`
          returns the full cards.
        - **"get"** : one property (full card), by internal `property_name`.
        - **"create"** : create a property. Requires `definition` — at minimum
          {"name", "label", "type", "fieldType", "groupName"}, plus "options"
          ([{"label", "value"}]) for an enumeration.
        - **"update"** : PATCH a property (e.g. add options). Requires
          `property_name` + `definition`.
        - **"delete"** : archive a property. Requires `property_name`. Supports
          `dry_run`.
        - **"groups"** : the property groups (the tabs of a record page).

        Args:
            op: list | get | create | update | delete | groups.
            object_type: contacts | companies | deals | tickets (or a custom
                object's name). Required for every op.
            property_name: the INTERNAL name — required for get, update, delete.
            definition: op="create"/"update" — the property definition dict.
            archived: op="list"/"get" — return archived properties instead.
            dry_run: op="delete" — report the property that would be archived
                without archiving it.
            names: op="list" — only these internal names.
            group: op="list" — only this groupName (see op="groups").
            include_hidden: op="list" — keep the hidden HubSpot-internal ones.
            custom_only: op="list" — only the portal's own properties (drops
                hubspotDefined ones).
            verbose: op="list" — full HubSpot cards instead of compact rows.
        """
        c = _client()
        try:
            return _property_op(c, op, object_type, property_name, definition,
                                archived, dry_run, names, group, include_hidden,
                                custom_only, verbose)
        except UpstreamHTTPError as e:
            refus = translate(e, object_type=object_type, object_id=property_name,
                              family="properties", what="property")
            if refus is None:
                raise
            raise refus from None

    def _property_list(c, otype, archived, names, group, include_hidden,
                       custom_only, verbose) -> dict:
        """op='list': filter, fill the pipeline-backed options, then project.

        The filters run on the FULL cards (`hidden`/`hubspotDefined` are not in the
        compact row), and the answer names what they dropped."""
        raw = c.list_properties(otype, archived=archived) or {}
        kept, dropped = filter_properties(
            list(raw.get("results") or []), names=names, group=group,
            include_hidden=include_hidden, custom_only=custom_only)
        fill_pipeline_options(kept, otype, c)
        out = {k: v for k, v in raw.items() if k != "results"}
        out["results"] = kept if verbose else [compact_property(p) for p in kept]
        if names:
            absent = sorted(set(names) - {p.get("name") for p in kept})
            if absent:
                out["unknown_names"] = absent
        out["projection"] = {"compact": not verbose, "dropped": dropped,
                             "hint": ("verbose=true for full cards; "
                                      "include_hidden=true for hidden ones")}
        return out

    def _property_op(c, op, object_type, property_name, definition, archived,
                     dry_run, names, group, include_hidden, custom_only,
                     verbose) -> dict:
        if op == "list":
            return _property_list(c, _need(object_type, "object_type", op), archived,
                                  names, group, include_hidden, custom_only, verbose)

        if op == "get":
            card = c.get_property(
                _need(object_type, "object_type", op),
                _need(property_name, "property_name", op), archived=archived)
            if isinstance(card, dict):
                fill_pipeline_options([card], object_type, c)
            return card

        if op == "create":
            return c.create_property(
                _need(object_type, "object_type", op),
                _payload(definition, "definition", op))

        if op == "update":
            return c.update_property(
                _need(object_type, "object_type", op),
                _need(property_name, "property_name", op),
                _payload(definition, "definition", op))

        if op == "delete":
            otype = _need(object_type, "object_type", op)
            pname = _need(property_name, "property_name", op)
            if dry_run:
                return {"dry_run": True, "would": "delete", "object_type": otype,
                        "property_name": pname,
                        "current": c.get_property(otype, pname)}
            return c.delete_property(otype, pname)

        if op == "groups":
            return c.list_property_groups(_need(object_type, "object_type", op))

        raise _bad("op must be 'list', 'get', 'create', 'update', 'delete' "
                   "or 'groups'")

    @mcp.tool()
    def hubspot_owners() -> dict:
        """List HubSpot owners (users) — to assign records by ownerId."""
        try:
            return _client().list_owners()
        except UpstreamHTTPError as e:
            refus = translate(e, family="owners", what="owner")
            if refus is None:
                raise
            raise refus from None
