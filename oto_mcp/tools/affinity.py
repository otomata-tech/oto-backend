"""Affinity — relationship CRM: persons, companies, opportunities, lists, field
values, notes, interactions, relationship strength.

Wraps `oto.tools.affinity.AffinityClient` (API v1 + v2, one Bearer key resolved
per call via `access.resolve_api_key("affinity")`). The key belongs to ONE
Affinity user and acts as them: what this connector writes is attributed to
the person who created the key, and their sharing rules decide what it sees.

**Surface** — one tool per object, the verb in `op`, the default op a READ:
- `affinity_entity` — persons, companies, opportunities: search, get (with
  field values), the lists an entity is on, relationship strength, create,
  update (core attributes and global field values);
- `affinity_list` — lists, their fields, saved views, dropdown options;
- `affinity_list_entry` — the rows of a list: query, get, add, remove, and
  `set_fields` (one entry or a batch of entries, field names and dropdown
  TEXT resolved here);
- `affinity_note` — notes on persons, companies, opportunities;
- `affinity_interactions` — emails, meetings, calls, chat messages with one
  external person, company or opportunity (read-only).

Field values come back flattened by default (`fields: {name: value}`, dropdowns
as their text); `full=true` returns Affinity's raw shape. Writes that change or
remove data take `dry_run=true`, which reads the current state and returns the
diff without writing.

Deep usage (tool order, ids, errors): `oto_guide op=read slug="affinity-playbook"`.

Client calls are written out as `_client().method(…)`: that is what makes them
checkable by the version-skew probe (`test_tools_client_methods_exist`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

_NAME = "affinity"
_GUIDE = 'oto_guide op=read slug="affinity-playbook"'

# Entries per `set_fields` batch: each entry is one PATCH (plus option lookups
# cached across the batch), and the REST invoke path stops at 45 s.
_BATCH_MAX = 25
# Field categories Affinity computes itself: never written.
_READ_ONLY_FIELD_TYPES = frozenset({"enriched", "relationship-intelligence", "hidden"})
_DEFAULT_ENTITY_FIELDS = ["global", "enriched", "relationship-intelligence"]
_DEFAULT_ENTRY_FIELDS = ["list"]
_INTERACTION_DEFAULT_DAYS = 90

Kind = Literal["person", "company", "opportunity"]


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, **provided) -> None:
    """An argument this op does not use is an intent error, not a silent no-op."""
    for name, value in provided.items():
        if value is not None and value is not False:
            raise _bad(f"op='{op}' does not use `{name}`.")


def _need(value, name: str, op: str):
    if value is None or value == "" or value == {} or value == []:
        raise _bad(f"op='{op}' requires `{name}`.")
    return value


def _upstream_message(e) -> str:
    status, body = e.status_code, str(e.body)[:400]
    if status == 401:
        return ("Affinity refused the API key (401): it is invalid or revoked. Create "
                "a new one in Affinity → Settings → Manage Apps.")
    if status == 403:
        return ("Affinity refused this action (403). The key's user lacks a "
                "permission: reading list entries and writing list fields need "
                "\"Export data from Lists\"; writing global fields needs \"Edit Global "
                "Field Values\"; the plan must include API access (Scale, Advanced or "
                f"Enterprise). Upstream: {body}")
    if status == 404:
        return ("Affinity: not found (404), or not visible to the key's user. "
                f"Upstream: {body}")
    if status == 429:
        return ("Affinity rate limit reached (429): 900 requests per user per minute, "
                "and a monthly account quota shared with every other Affinity "
                f"integration. Upstream: {body}")
    if status >= 500:
        return f"Affinity is temporarily unavailable (HTTP {status}); retry later."
    return f"Affinity refused the request (HTTP {status}): {body}"


def _fatal(e) -> bool:
    """Errors that would fail every remaining item of a batch the same way."""
    from oto.tools.common.errors import UpstreamHTTPError
    import requests

    if isinstance(e, UpstreamHTTPError):
        return e.status_code in (401, 403, 429)
    return isinstance(e, (requests.ConnectionError, requests.Timeout))


# --- flattened field values ----------------------------------------------------

def _person_name(p: dict) -> str:
    name = " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x)
    return name or p.get("primaryEmailAddress") or str(p.get("id"))


def _plain(value: Any) -> Any:
    """Affinity's typed value `{type, data}` as what a person would read."""
    if not isinstance(value, dict) or "type" not in value:
        return value
    vtype, data = value.get("type", ""), value.get("data")
    if data is None:
        return None
    if vtype in ("dropdown", "ranked-dropdown"):
        return data.get("text")
    if vtype == "dropdown-multi":
        return [d.get("text") for d in data]
    if vtype == "person":
        return _person_name(data)
    if vtype == "person-multi":
        return [_person_name(p) for p in data]
    if vtype == "company":
        return data.get("name")
    if vtype == "company-multi":
        return [co.get("name") for co in data]
    if vtype == "location":
        return ", ".join(v for v in (data.get("city"), data.get("state"),
                                     data.get("country")) if v) or None
    if vtype == "location-multi":
        return [_plain({"type": "location", "data": d}) for d in data]
    return data


def _flatten(record: Any) -> Any:
    """Replace a `fields` array by `{name: plain value}`, at any depth."""
    if isinstance(record, list):
        return [_flatten(r) for r in record]
    if not isinstance(record, dict):
        return record
    out = {}
    for k, v in record.items():
        if k == "fields" and isinstance(v, list) and all(isinstance(f, dict) and "name" in f
                                                          for f in v):
            out[k] = {f["name"]: _plain(f.get("value")) for f in v}
        else:
            out[k] = _flatten(v)
    return out


def _view(payload: Any, full: bool) -> Any:
    if full:
        return payload
    from oto.tools.affinity.client import next_cursor

    out = _flatten(payload)
    if isinstance(out, dict) and isinstance(out.get("pagination"), dict):
        out["next_cursor"] = next_cursor(payload)
        out.pop("pagination", None)
    return out


def _slim_notes(payload: Any, full: bool) -> Any:
    """Note list: each body becomes its LENGTH (`op=get` reads it), never an extract."""
    if full or not isinstance(payload, dict):
        return payload
    rows = []
    for note in payload.get("data") or []:
        row = dict(note)
        html = (row.pop("content", None) or {}).get("html") or ""
        row["content_length"] = len(html)
        rows.append(row)
    return {**payload, "data": rows,
            "projection": {"omitted": ["content"], "hint": "op='get' or full=true for bodies."}}


def _slim_people(value: Any) -> Any:
    """Drop every person's full `emails` list when a `primary_email` is there."""
    if isinstance(value, list):
        return [_slim_people(v) for v in value]
    if not isinstance(value, dict):
        return value
    drop = "emails" if "primary_email" in value else None
    return {k: _slim_people(v) for k, v in value.items() if k != drop}


# --- field resolution ------------------------------------------------------------

def _resolve_field(meta: list[dict], key: str) -> dict:
    """A field by id (`field-123`) or by name (case-insensitive, must be unique)."""
    for f in meta:
        if f.get("id") == key:
            return f
    named = list({f.get("id"): f for f in meta
                  if (f.get("name") or "").strip().lower() == key.strip().lower()}.values())
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise _bad(f"Several fields are named {key!r}: "
                   + ", ".join(f"{f['id']}" for f in named) + ". Pass the id.")
    names = ", ".join(sorted(f"{f.get('name')} ({f.get('id')})" for f in meta))
    raise _bad(f"No field {key!r}. Available: {names}")


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) or (
        isinstance(value, str) and value.strip().isdigit())


class _Options:
    """Dropdown options per field, fetched once per call and matched by text."""

    def __init__(self, fetch):
        self._fetch, self._cache = fetch, {}

    def option_id(self, field: dict, text: str) -> int:
        fid = field["id"]
        if fid not in self._cache:
            self._cache[fid] = self._fetch(fid)
        wanted = text.strip().lower()
        for o in self._cache[fid]:
            if (o.get("text") or "").strip().lower() == wanted:
                return o["id"]
        known = ", ".join(repr(o.get("text")) for o in self._cache[fid])
        raise _bad(f"Field {field.get('name')!r} has no option {text!r}. Options: {known}")


def _write_value(field: dict, value: Any, options: _Options) -> dict:
    """`{type, data}` for `value` on `field`, option text resolved to its id."""
    from oto.tools.affinity.client import field_value

    if field.get("type") in _READ_ONLY_FIELD_TYPES:
        raise _bad(f"Field {field.get('name')!r} is {field.get('type')}: Affinity computes "
                   "it, it cannot be written.")
    vtype = field.get("valueType")
    if vtype in ("dropdown", "ranked-dropdown", "dropdown-multi") and value is not None:
        values = value if isinstance(value, list) else [value]
        ids = [v if _is_id(v) or isinstance(v, dict) else options.option_id(field, str(v))
               for v in values]
        value = ids if vtype == "dropdown-multi" else ids[0]
    try:
        return field_value(vtype, value)
    except ValueError as e:
        raise _bad(f"Field {field.get('name')!r}: {e}")


def _all_pages(fetch) -> list:
    from oto.tools.affinity.client import next_cursor

    out, cursor, seen = [], None, set()
    for _ in range(50):
        page = fetch(cursor)
        out.extend(page.get("data") or [])
        cursor = next_cursor(page)
        if not cursor or cursor in seen:
            return out
        seen.add(cursor)
    return out


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_domain(domain: Optional[str]) -> str:
    d = (domain or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0]
    return d[4:] if d.startswith("www.") else d


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """« Test the connection »: `GET /v2/auth/whoami` then `GET /auth/whoami` (v1),
    both free of the monthly quota.

    It proves the key authenticates on both API generations. It cannot prove the role permissions
    ("Export data from Lists", "Edit Global Field Values"): Affinity does not
    expose them there, and the first refused call names them instead. A grant
    whose scopes are read-only (`api.read`) is reported as such.
    """
    from oto.tools.affinity import AffinityClient
    from oto.tools.common.errors import UpstreamHTTPError

    client = AffinityClient(api_key=fields.get("key"))
    try:
        who = client.whoami() or {}
        client.whoami_v1()  # most writes go through v1: both must accept the key
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Affinity HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Affinity HTTP {e.status_code}: {e.body}")
    scopes = set((who.get("grant") or {}).get("scopes") or [])
    if scopes and not scopes & {"api", "mcp"}:
        raise connector_verify.NonAutorise(
            f"The Affinity key is read-only (scopes: {sorted(scopes)}): writes will fail.")


def register(mcp: FastMCP) -> None:
    from oto.tools.affinity import AffinityClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> AffinityClient:
        key, _ = access.resolve_api_key(_NAME)
        return AffinityClient(api_key=key)

    def _run(fn):
        try:
            return fn()
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except ValueError as e:
            raise _bad(str(e))

    def _field_meta_for_list(list_id) -> list:
        return _run(lambda: _all_pages(
            lambda cur: _client().list_fields(list_id, cursor=cur, limit=100)))

    def _field_meta_for_kind(kind) -> list:
        return _run(lambda: _all_pages(
            lambda cur: _client().global_fields(kind, cursor=cur, limit=100)))

    def _option_fetcher(list_id=None, kind=None):
        return lambda fid: _run(lambda: _all_pages(
            lambda cur: _client().dropdown_options(fid, list_id=list_id, kind=kind,
                                                   cursor=cur, limit=100)))

    def _field_selection(fields: Optional[list], default: list) -> dict:
        chosen = fields or default
        types = [f for f in chosen if f in ("enriched", "global", "list",
                                            "relationship-intelligence")]
        ids = [f for f in chosen if f not in types]
        return {"field_types": types or None, "field_ids": ids or None}

    # --- entities ----------------------------------------------------------------

    @mcp.tool()
    def affinity_entity(
        op: Literal["search", "get", "entries", "relationships", "create",
                    "update"] = "search",
        kind: Kind = "company",
        id: Optional[int] = None,
        term: Optional[str] = None,
        fields: Optional[list[str]] = None,
        item: Optional[dict] = None,
        dry_run: bool = False,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """Affinity persons, companies and opportunities.

        `op`:
        - `search` — persons (name or email) or companies (name or domain)
          containing `term`. Opportunities are found through their list
          (`affinity_list_entry op=query`). Paginate with `cursor` (the
          `next_page_token` of the previous answer).
        - `get` — one record (`id`) with its field values, flattened to
          `{name: value}`. `fields` picks field ids or categories (global,
          enriched, relationship-intelligence); list fields are read via
          `op=entries`.
        - `entries` — the lists a person/company is on, with ALL field values.
        - `relationships` — who in your team knows this person/company, with
          `interactionScore` (0-1, ≥ 0.7 ≈ regular contact).
        - `create` — `item`: person {first_name, last_name, emails,
          organization_ids}; company {name, domain, person_ids} (an existing
          company with the same domain is RETURNED instead, unless
          `allow_duplicate: true`); opportunity {name, list_id, person_ids,
          organization_ids}.
        - `update` — `id` + `item` with the same keys, plus `fields`: {field name
          or id: value} for global fields (dropdowns accept the option text).
          ⚠️ Arrays (emails, organization_ids, person_ids) REPLACE the current
          set. `dry_run=true` returns the diff without writing.

        Writes are attributed to the Affinity user who owns the key.
        Details: oto_guide op=read slug="affinity-playbook".

        Args:
            op: search | get | entries | relationships | create | update.
            kind: person | company | opportunity.
            id: the record id (get, entries, relationships, update).
            term: search text.
            fields: get — field ids or categories to include.
            item: create/update — the attributes (see above).
            dry_run: create/update — validate and preview, write nothing.
            limit: page size (1-100).
            cursor: next page token from the previous answer.
            full: raw Affinity shapes instead of flattened field values.
        """
        if op == "search":
            _need(term, "term", op)
            _refuse_ignored(op, id=id, fields=fields, item=item, dry_run=dry_run)
            if kind == "opportunity":
                raise _bad("Opportunities are searched through their list: "
                           "affinity_list_entry(op='query', list_id=…).")
            if kind == "person":
                return _run(lambda: _client().search_persons(
                    term, page_size=limit, page_token=cursor))
            return _run(lambda: _client().search_organizations(
                term, page_size=limit, page_token=cursor))
        if op in ("get", "entries", "relationships"):
            _need(id, "id", op)
            _refuse_ignored(op, term=term, item=item, dry_run=dry_run)
            if op == "get":
                if kind == "opportunity":
                    _refuse_ignored(op, fields=fields)
                    return _view(_run(lambda: _client().get_opportunity(id)), full)
                sel = _field_selection(fields, _DEFAULT_ENTITY_FIELDS)
                if kind == "person":
                    return _view(_run(lambda: _client().get_person(id, **sel)), full)
                return _view(_run(lambda: _client().get_company(id, **sel)), full)
            _refuse_ignored(op, fields=fields)
            if op == "entries":
                return _view(_run(lambda: _client().entity_list_entries(
                    kind, id, cursor=cursor, limit=limit)), full)
            return _view(_run(lambda: _client().relationships(
                kind, id, cursor=cursor, limit=limit)), full)
        if op == "create":
            _need(item, "item", op)
            _refuse_ignored(op, id=id, term=term, fields=fields)
            return _create(kind, dict(item), dry_run)
        if op == "update":
            _need(id, "id", op)
            _need(item, "item", op)
            _refuse_ignored(op, term=term, fields=fields)
            return _update(kind, id, dict(item), dry_run)
        raise _bad(f"invalid `op`: {op!r}.")

    def _take(item: dict, allowed: set, what: str) -> dict:
        unknown = set(item) - allowed
        if unknown:
            raise _bad(f"{what}: unknown keys {sorted(unknown)}. Accepted: {sorted(allowed)}.")
        return item

    def _create(kind: str, item: dict, dry_run: bool) -> Any:
        if kind == "person":
            _take(item, {"first_name", "last_name", "emails", "organization_ids"}, "person")
            if dry_run:
                return {"dry_run": True, "would_create": item}
            return _run(lambda: _client().create_person(
                item.get("first_name"), item.get("last_name"), item.get("emails"),
                item.get("organization_ids")))
        if kind == "company":
            _take(item, {"name", "domain", "person_ids", "allow_duplicate"}, "company")
            allow = bool(item.pop("allow_duplicate", False))
            domain = _norm_domain(item.get("domain"))
            if domain and not allow:
                found = _run(lambda: _client().search_organizations(domain))
                for org in (found or {}).get("organizations") or []:
                    known = {_norm_domain(d) for d in (org.get("domains") or [])}
                    known.add(_norm_domain(org.get("domain")))
                    if domain in known:
                        return {"existing": True, "company": org,
                                "hint": "Not created: this domain is already in Affinity. "
                                        "Pass allow_duplicate: true to create anyway."}
            if dry_run:
                return {"dry_run": True, "would_create": item}
            return _run(lambda: _client().create_organization(
                item.get("name"), item.get("domain"), item.get("person_ids")))
        _take(item, {"name", "list_id", "person_ids", "organization_ids"}, "opportunity")
        if dry_run:
            return {"dry_run": True, "would_create": item}
        return _run(lambda: _client().create_opportunity(
            item.get("name"), item.get("list_id"), item.get("person_ids"),
            item.get("organization_ids")))

    _UPDATE_KEYS = {
        "person": {"first_name", "last_name", "emails", "organization_ids", "fields"},
        "company": {"name", "domain", "person_ids", "fields"},
        "opportunity": {"name", "person_ids", "organization_ids"},
    }
    # v1 attribute → where its current value is read in the v2 record.
    _CURRENT = {"first_name": "firstName", "last_name": "lastName", "name": "name",
                "domain": "domain"}
    # Replaced as a whole by a v1 update; their current value is read from v1.
    _ARRAYS = {"emails", "organization_ids", "person_ids"}

    def _update(kind: str, entity_id: int, item: dict, dry_run: bool) -> Any:
        _take(item, _UPDATE_KEYS[kind], kind)
        field_values = item.pop("fields", None) or {}
        updates = []
        if field_values:
            meta = _field_meta_for_kind(kind)
            options = _Options(_option_fetcher(kind=kind))
            for key, value in field_values.items():
                f = _resolve_field(meta, key)
                updates.append({"id": f["id"], "name": f.get("name"), "asked": value,
                                "value": _write_value(f, value, options)})
        if not item and not updates:
            raise _bad("Nothing to update.")
        if dry_run:
            ids = [u["id"] for u in updates] or None
            if kind == "person":
                current = _run(lambda: _client().get_person(entity_id, field_ids=ids))
            elif kind == "company":
                current = _run(lambda: _client().get_company(entity_id, field_ids=ids))
            else:
                current = _run(lambda: _client().get_opportunity(entity_id))
            now = {f.get("id"): _plain(f.get("value")) for f in current.get("fields") or []}
            changes = {k: {"from": current.get(_CURRENT.get(k, k)), "to": v}
                       for k, v in item.items() if k not in _ARRAYS}
            arrays = [k for k in item if k in _ARRAYS]
            if arrays:
                # v2 does not return association ids: read them from v1, which
                # is also what the update REPLACES.
                v1 = _run(lambda: _client().get_entity_v1(kind, entity_id))
                for k in arrays:
                    changes[k] = {"from": v1.get(k), "to": item[k], "replaces": True}
            for u in updates:
                changes[u["name"]] = {"from": now.get(u["id"]), "to": u["asked"]}
            return {"dry_run": True, "id": entity_id, "changes": changes}
        out: dict = {"id": entity_id}
        if item:
            if kind == "person":
                out["updated"] = _run(lambda: _client().update_person(entity_id, **item))
            elif kind == "company":
                out["updated"] = _run(lambda: _client().update_organization(entity_id, **item))
            else:
                out["updated"] = _run(lambda: _client().update_opportunity(entity_id, **item))
        if updates:
            payload = [{"id": u["id"], "value": u["value"]} for u in updates]
            _run(lambda: _client().update_entity_fields(kind, entity_id, payload))
            out["fields_updated"] = [u["name"] for u in updates]
        return out

    # --- lists ---------------------------------------------------------------------

    @mcp.tool()
    def affinity_list(
        op: Literal["list", "get", "fields", "views", "options"] = "list",
        list_id: Optional[int] = None,
        field_id: Optional[str] = None,
        kind: Optional[Literal["person", "company"]] = None,
        term: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """Affinity lists and their columns.

        `op`:
        - `list` — the lists the key's user can see (`term` filters by name).
        - `get` — one list (`list_id`): name, type (person, company or
          opportunity), owner.
        - `fields` — the list's columns: id, name, category (list, global,
          enriched…), `valueType`, `isRequired`.
        - `views` — the list's saved views (read their rows with
          `affinity_list_entry op=query view_id=…`).
        - `options` — the options of a dropdown field (`field_id`), on a list
          (`list_id`) or global (`kind` person|company).

        Args:
            op: list | get | fields | views | options.
            list_id: the list.
            field_id: options — the dropdown field.
            kind: options on a global field — person | company.
            term: list — name filter.
            limit: page size (1-100).
            cursor: next page cursor (`next_cursor` of the previous answer).
            full: fields — keep each column's `filterability` block.
        """
        if op == "list":
            _refuse_ignored(op, list_id=list_id, field_id=field_id, kind=kind)
            return _view(_run(lambda: _client().list_lists(
                term=term, cursor=cursor, limit=limit)), False)
        _refuse_ignored(op, term=term)
        if op == "options":
            _need(field_id, "field_id", op)
            if (list_id is None) == (kind is None):
                raise _bad("op='options' needs `list_id` (list field) or `kind` (global field).")
            return _view(_run(lambda: _client().dropdown_options(
                field_id, list_id=list_id, kind=kind, cursor=cursor, limit=limit)), False)
        _need(list_id, "list_id", op)
        _refuse_ignored(op, field_id=field_id, kind=kind)
        if op == "get":
            return _run(lambda: _client().get_list(list_id))
        if op == "fields":
            out = _view(_run(lambda: _client().list_fields(
                list_id, cursor=cursor, limit=limit)), False)
            return out if full else output_projection.project(
                out, items_path="data", item_drop=("filterability",))
        if op == "views":
            return _view(_run(lambda: _client().list_saved_views(
                list_id, cursor=cursor, limit=limit)), False)
        raise _bad(f"invalid `op`: {op!r}.")

    # --- list entries --------------------------------------------------------------

    @mcp.tool()
    def affinity_list_entry(
        op: Literal["query", "get", "add", "remove", "set_fields"] = "query",
        list_id: Optional[int] = None,
        entry_id: Optional[int] = None,
        entity_id: Optional[int] = None,
        view_id: Optional[int] = None,
        fields: Optional[list[str]] = None,
        item: Optional[dict] = None,
        items: Optional[list[dict]] = None,
        dry_run: bool = False,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """The rows of an Affinity list (`list_id`) and their field values.

        `op`:
        - `query` — rows with their list fields, flattened to `{name: value}`;
          `view_id` reads a saved view instead (its own filters and columns);
          `fields` picks field ids or categories (list, global, enriched,
          relationship-intelligence). Paginate with `cursor`.
        - `get` — one row (`entry_id`).
        - `add` — put a person or company (`entity_id`) on the list; skipped if
          it is already there. Opportunities are created ON their list with
          `affinity_entity op=create kind=opportunity`.
        - `remove` — take a row (`entry_id`) off the list, with its list field
          values. Refused on opportunity lists (it would delete the deal).
        - `set_fields` — `item` {entry_id, fields: {field name or id: value}} or
          `items` [same, …] (≤ 25 entries). Dropdowns accept the option TEXT,
          persons/companies their id, `null` clears. `dry_run=true` returns
          the diff per entry without writing.

        A row id (`entry_id`) is not the person/company id (`entity_id`).
        Details: oto_guide op=read slug="affinity-playbook".

        Args:
            op: query | get | add | remove | set_fields.
            list_id: the list (always required).
            entry_id: get/remove — the row.
            entity_id: add — the person or company id.
            view_id: query — a saved view of the list.
            fields: query/get — field ids or categories to include.
            item: set_fields — one entry's update.
            items: set_fields — several entries' updates.
            dry_run: remove/set_fields — preview, write nothing.
            limit: page size (1-100).
            cursor: next page cursor.
            full: raw Affinity shapes instead of flattened field values.
        """
        _need(list_id, "list_id", op)
        if op == "query":
            _refuse_ignored(op, entry_id=entry_id, entity_id=entity_id, item=item,
                            items=items, dry_run=dry_run)
            if view_id is not None:
                _refuse_ignored(op, fields=fields)
                return _view(_run(lambda: _client().saved_view_entries(
                    list_id, view_id, cursor=cursor, limit=limit)), full)
            sel = _field_selection(fields, _DEFAULT_ENTRY_FIELDS)
            return _view(_run(lambda: _client().list_entries(
                list_id, cursor=cursor, limit=limit, **sel)), full)
        _refuse_ignored(op, view_id=view_id, cursor=cursor, limit=limit)
        if op == "get":
            _need(entry_id, "entry_id", op)
            _refuse_ignored(op, entity_id=entity_id, item=item, items=items, dry_run=dry_run)
            sel = _field_selection(fields, ["list", "global"])
            return _view(_run(lambda: _client().get_list_entry(list_id, entry_id, **sel)), full)
        _refuse_ignored(op, fields=fields)
        if op == "add":
            _need(entity_id, "entity_id", op)
            _refuse_ignored(op, entry_id=entry_id, item=item, items=items, dry_run=dry_run)
            lst = _run(lambda: _client().get_list(list_id))
            ltype = lst.get("type")
            if ltype == "opportunity":
                raise _bad("This is an opportunity list: create the opportunity on it with "
                           "affinity_entity(op='create', kind='opportunity').")
            current = _run(lambda: _all_pages(lambda cur: _client().entity_list_entries(
                ltype, entity_id, cursor=cur, limit=100)))
            for e in current:
                if e.get("listId") == list_id:
                    return {"already_on_list": True, "entry_id": e.get("id")}
            return _run(lambda: _client().add_list_entry(list_id, entity_id))
        if op == "remove":
            _need(entry_id, "entry_id", op)
            _refuse_ignored(op, entity_id=entity_id, item=item, items=items)
            lst = _run(lambda: _client().get_list(list_id))
            if lst.get("type") == "opportunity":
                raise _bad("Refused: removing a row from an opportunity list deletes the "
                           "opportunity itself. Do it in Affinity if that is intended.")
            if dry_run:
                entry = _run(lambda: _client().get_list_entry(
                    list_id, entry_id, field_types=["list"]))
                return {"dry_run": True, "would_remove": _flatten(entry),
                        "note": "The row's list field values are deleted with it."}
            return _run(lambda: _client().remove_list_entry(list_id, entry_id))
        if op == "set_fields":
            _refuse_ignored(op, entry_id=entry_id, entity_id=entity_id)
            if (item is None) == (items is None):
                raise _bad("op='set_fields' takes exactly one of `item` or `items`.")
            batch = [item] if item is not None else items
            if len(batch) > _BATCH_MAX:
                raise _bad(f"Too many entries ({len(batch)}): at most {_BATCH_MAX} per call.")
            meta = _field_meta_for_list(list_id)
            options = _Options(_option_fetcher(list_id=list_id))
            prepared = [_prepare_entry(i, u, meta, options) for i, u in enumerate(batch)]
            if dry_run:
                previews = [_entry_diff(list_id, p) for p in prepared]
                return {"dry_run": True, **(previews[0] if item is not None
                                            else {"entries": previews})}
            if item is not None:
                p = prepared[0]
                _run(lambda: _client().update_list_entry_fields(
                    list_id, p["entry_id"], p["updates"]))
                return {"entry_id": p["entry_id"], "fields_updated": p["names"]}
            return _batch_write(list_id, prepared)
        raise _bad(f"invalid `op`: {op!r}.")

    def _prepare_entry(index: int, update: dict, meta: list, options: _Options) -> dict:
        if not isinstance(update, dict):
            raise _bad(f"items[{index}] must be an object {{entry_id, fields}}.")
        unknown = set(update) - {"entry_id", "fields"}
        if unknown:
            raise _bad(f"items[{index}]: unknown keys {sorted(unknown)} (entry_id, fields).")
        entry = update.get("entry_id")
        values = update.get("fields") or {}
        if entry is None or not values:
            raise _bad(f"items[{index}] needs `entry_id` and a non-empty `fields`.")
        resolved = [(_resolve_field(meta, str(k)), v) for k, v in values.items()]
        return {"index": index, "entry_id": entry,
                "names": [f.get("name") for f, _ in resolved],
                "asked": [v for _, v in resolved],
                "updates": [{"id": f["id"], "value": _write_value(f, v, options)}
                            for f, v in resolved]}

    def _entry_diff(list_id, prepared: dict) -> dict:
        ids = [u["id"] for u in prepared["updates"]]
        entry = _run(lambda: _client().get_list_entry(
            list_id, prepared["entry_id"], field_ids=ids))
        fields = (entry.get("entity") or {}).get("fields") or entry.get("fields") or []
        now = {f.get("id"): _plain(f.get("value")) for f in fields}
        return {"entry_id": prepared["entry_id"], "changes": {
            name: {"from": now.get(u["id"]), "to": asked}
            for name, u, asked in zip(prepared["names"], prepared["updates"],
                                      prepared["asked"])}}

    def _batch_write(list_id, prepared: list) -> dict:
        c = _client()
        failed, done = [], 0
        for p in prepared:
            try:
                c.update_list_entry_fields(list_id, p["entry_id"], p["updates"])
                done += 1
            except Exception as e:  # noqa: BLE001 — per-entry receipt, fatal re-raised
                if _fatal(e):
                    if isinstance(e, UpstreamHTTPError):
                        raise _bad(f"Batch stopped at entry {p['index']} after {done} "
                                   f"written: {_upstream_message(e)}")
                    raise _bad(f"Batch stopped at entry {p['index']} after {done} "
                               f"written: {e}")
                msg = _upstream_message(e) if isinstance(e, UpstreamHTTPError) else str(e)
                failed.append({"index": p["index"], "entry_id": p["entry_id"], "error": msg})
        return {"total": len(prepared), "succeeded": done, "failed": failed,
                "rate_limit": c.last_rate_limit or None}

    # --- notes ---------------------------------------------------------------------

    @mcp.tool()
    def affinity_note(
        op: Literal["list", "get", "create", "update", "delete"] = "list",
        note_id: Optional[int] = None,
        kind: Optional[Kind] = None,
        entity_id: Optional[int] = None,
        content: Optional[str] = None,
        person_ids: Optional[list[int]] = None,
        company_ids: Optional[list[int]] = None,
        opportunity_ids: Optional[list[int]] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        dry_run: bool = False,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """Affinity notes.

        `op`:
        - `list` — notes of one record (`kind` + `entity_id`: direct notes, notes
          on its meetings, mentions), or all notes; `since`/`until` bound the
          creation date (ISO 8601). Bodies are listed as their length;
          `full=true` or `op=get` returns them.
        - `get` — one note (`note_id`).
        - `create` — `content` (plain text, or HTML with p, br, strong, em, u,
          ol, ul, li, a) attached to at least one of `person_ids`,
          `company_ids`, `opportunity_ids`.
        - `update` — new `content` and/or associations; an association list
          REPLACES that kind (`[]` clears it). Notes with @mentions cannot be
          edited.
        - `delete` — `dry_run=true` shows the note instead.

        Notes are authored by the Affinity user who owns the key.

        Args:
            op: list | get | create | update | delete.
            note_id: get/update/delete — the note.
            kind: list — person | company | opportunity.
            entity_id: list — the record id.
            content: create/update — the body.
            person_ids: create/update — attached persons.
            company_ids: create/update — attached companies.
            opportunity_ids: create/update — attached opportunities.
            since: list — created at or after (ISO 8601).
            until: list — created before (ISO 8601).
            dry_run: delete — show what would be deleted.
            limit: page size (1-100).
            cursor: next page cursor.
            full: list — include note bodies.
        """
        refs = dict(person_ids=person_ids, company_ids=company_ids,
                    opportunity_ids=opportunity_ids)
        if op == "list":
            _refuse_ignored(op, note_id=note_id, content=content, dry_run=dry_run, **refs)
            if (kind is None) != (entity_id is None):
                raise _bad("op='list' takes `kind` and `entity_id` together, or neither.")
            parts = [f"createdAt>={since}" if since else None,
                     f"createdAt<{until}" if until else None]
            flt = " & ".join(p for p in parts if p) or None
            return _slim_notes(_view(_run(lambda: _client().list_notes(
                kind=kind, entity_id=entity_id, filter=flt, cursor=cursor, limit=limit)),
                False), full)
        _refuse_ignored(op, kind=kind, entity_id=entity_id, since=since, until=until,
                        limit=limit, cursor=cursor, full=full)
        if op == "create":
            _need(content, "content", op)
            _refuse_ignored(op, note_id=note_id, dry_run=dry_run)
            return _run(lambda: _client().create_note(content, **refs))
        _need(note_id, "note_id", op)
        if op == "get":
            _refuse_ignored(op, content=content, dry_run=dry_run, **refs)
            return _run(lambda: _client().get_note(note_id))
        if op == "update":
            _refuse_ignored(op, dry_run=dry_run)
            _run(lambda: _client().update_note(note_id, content=content, **refs))
            return {"note_id": note_id, "updated": True}
        if op == "delete":
            _refuse_ignored(op, content=content, **refs)
            if dry_run:
                return {"dry_run": True, "would_delete": _run(
                    lambda: _client().get_note(note_id))}
            _run(lambda: _client().delete_note(note_id))
            return {"note_id": note_id, "deleted": True}
        raise _bad(f"invalid `op`: {op!r}.")

    # --- interactions --------------------------------------------------------------

    @mcp.tool()
    def affinity_interactions(
        kind: Kind,
        id: int,
        type: Literal["email", "meeting", "call", "chat"] = "email",
        since: Optional[str] = None,
        until: Optional[str] = None,
        direction: Optional[Literal["sent", "received"]] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """Emails, meetings, calls or chat messages your team had with ONE
        external person, company or opportunity — read-only, metadata only
        (subjects, participants, dates; never email bodies).

        Window: `since`/`until` (ISO 8601), at most one year apart; default the
        last 90 days. `direction` (email/chat): sent | received. Paginate with
        `cursor` (`next_page_token` of the previous answer). Participants carry
        their primary email only; `full=true` returns all their addresses. An internal person
        (a user of your Affinity) cannot be the subject.

        Args:
            kind: person | company | opportunity.
            id: the record id.
            type: email | meeting | call | chat.
            since: window start (ISO 8601); default 90 days ago.
            until: window end (ISO 8601); default now.
            direction: email/chat — sent | received.
            limit: page size (max 100).
            cursor: next page token.
            full: all participant email addresses.
        """
        anchor = {"person": "person_id", "company": "organization_id",
                  "opportunity": "opportunity_id"}[kind]
        start = since or _since(_INTERACTION_DEFAULT_DAYS)
        end = until or _since(0)
        out = _run(lambda: _client().list_interactions(
            type, start_time=start, end_time=end, direction=direction,
            page_size=limit, page_token=cursor, **{anchor: id}))
        return out if full else _slim_people(out)
