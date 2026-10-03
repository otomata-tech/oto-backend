"""Amplitude — product analytics, READ ONLY: taxonomy, Dashboard REST queries,
saved charts, users, behavioral cohorts.

Wraps `oto.tools.amplitude.AmplitudeClient` (HTTP Basic, project API key +
secret key). Credential resolved per call via
`access.resolve_credential_fields("amplitude")` (byo user OR org, no platform
key): `api_key`, `secret_key` (both secret) and `region` (us by default, eu).
One key pair = one project: there is no project parameter anywhere.

Five tools, verb in `op=` (ADR 0047), no argument silently ignored
(`_refuse_ignored`):
- `amplitude_schema` — what the project tracks (read before any query);
- `amplitude_chart` — a saved chart, computed by Amplitude (the team's number);
- `amplitude_query` — segmentation, funnel, retention, active users, sessions;
- `amplitude_user` — find a user, read their event stream;
- `amplitude_cohort` — list cohorts, request a download, read its members.

**What the tool layer adds to the transport:**
1. **Cost guard.** The Dashboard API bills a cost budget per project (108,000
   per hour, cost = days × conditions × query type) and 5 concurrent queries.
   A window over `LONG_RANGE_DAYS` needs `long_range=True`, and `MAX_RANGE_DAYS`
   is a hard cap, so one careless query cannot spend the team's hour.
2. **Events as names.** `"Sign Up"` is accepted wherever an event object is,
   and wrapped as `{"event_type": "Sign Up"}`.
3. **The auth trap, named.** Bad credentials are a 403 whose `details` differ
   by cause: "Invalid API Key" (unknown key → usually the other region) vs
   "Invalid API/Secret Key combination" (wrong or missing secret). The refusal
   says which setting to change.
4. **Slim views** by default, `full=True` returns the raw payload.
5. **Bounded payloads.** Cohort members and chart CSVs are returned as a
   bounded preview with their total, never the whole file.

Client calls are written plainly (`_client().list_event_types()`) for the
version probe (`test_tools_client_methods_exist`).

Live-tested on 2026-10-02 against a real US project with no events: auth,
the 403 diagnosis, taxonomy, every query op, user search, cohort list and
the chart endpoints (`/query` and `/csv` both answer 404 "Chart not found"
for an unknown id). Not exercised on real data: series content, user
activity, cohort download.
"""
from __future__ import annotations

import time
from datetime import date
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

if TYPE_CHECKING:  # `_client()` return annotation only — never evaluated
    from oto.tools.amplitude import AmplitudeClient

#: Where the user finds the key pair, in THEIR Amplitude organization.
WHERE_TO_FIND = "Amplitude → Settings → Organization settings → Projects → <project> → General"

LONG_RANGE_DAYS = 92
MAX_RANGE_DAYS = 366
MAX_SEGMENTS = 5
MAX_FUNNEL_STEPS = 10
#: Lines of a cohort file / chart CSV returned to the agent.
PREVIEW_LINES = 100
#: How long `amplitude_cohort(op="request")` waits for the job (REST timeout is 45 s).
COHORT_WAIT_S = 20

EventArg = Union[str, Dict[str, Any]]


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, hint: str, **provided: Any) -> None:
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op={op!r} does not use `{name}` — {hint}")


def _region(value: Any) -> str:
    from oto.tools.amplitude import REGIONS

    region = str(value or "us").strip().lower()
    if region not in REGIONS:
        raise _bad(f"Amplitude: unknown region {value!r} on this connector — "
                   f"expected one of {', '.join(REGIONS)}.")
    return region


def _details(e: Any) -> str:
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    return str(meta.get("details") or err.get("message") or body.get("error")
               or e.body or "").strip()


def upstream_message(e: Any, region: str = "us") -> str:
    """An Amplitude refusal (`UpstreamHTTPError`) as an actionable message."""
    status, detail = e.status_code, _details(e)
    if status in (401, 403):
        if "secret" in detail.lower():
            return (f"Amplitude refused the key pair ({status}: {detail}): the API "
                    f"key is known but the secret key is wrong or missing — copy "
                    f"both from {WHERE_TO_FIND}.")
        if "invalid api key" in detail.lower():
            other = "eu" if region == "us" else "us"
            return (f"Amplitude does not know this API key in the « {region} » "
                    f"region ({status}: {detail}). Most often the project lives in "
                    f"« {other} »: change the connector's region, or check the key "
                    f"in {WHERE_TO_FIND}.")
        return (f"Amplitude refused this read ({status}: {detail}) — the key pair "
                f"may lack access to this API on your plan.")
    if status == 400 and "propert" in detail.lower():
        return (f"Amplitude refused the query (400): {detail} — use the exact "
                f"names from `amplitude_schema` (custom user properties are "
                f"`gp:…`).")
    if status == 404:
        return f"Amplitude: not found (404){' — ' + detail if detail else ''}."
    return f"Amplitude refused the request (HTTP {status}): {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """« Test connection »: `GET /api/2/taxonomy/event` (cost 1, no side
    effect) on the declared region's host. A 401/403 is re-raised as
    `NonAutorise` carrying the region/secret diagnosis."""
    from oto.tools.amplitude import AmplitudeClient
    from oto.tools.common import UpstreamHTTPError

    region = _region(fields.get("region"))
    try:
        AmplitudeClient(fields["api_key"], fields["secret_key"],
                        region=region).list_event_types()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(upstream_message(e, region)) from None
        raise


def _client() -> AmplitudeClient:
    """The Amplitude client for THIS caller's credential, on its region's host."""
    from oto.tools.amplitude import AmplitudeClient

    fields = access.resolve_credential_fields("amplitude")
    return AmplitudeClient(fields["api_key"], fields["secret_key"],
                           region=_region(fields.get("region")))


def _run(fn) -> Any:
    """4xx → named refusal; 429 and 5xx stay typed (retryable)."""
    from oto.tools.common import UpstreamHTTPError

    try:
        return fn()
    except ValueError as e:
        raise _bad(str(e)) from None
    except UpstreamHTTPError as e:
        if 400 <= e.status_code < 500 and e.status_code != 429:
            region = "us"
            try:
                region = _region(access.resolve_credential_fields("amplitude").get("region"))
            except Exception:  # noqa: SILENT — the region only refines the message; the refusal is raised just below
                pass
            raise _bad(upstream_message(e, region)) from None
        raise


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def _event(value: Any, name: str) -> dict:
    if isinstance(value, str) and value.strip():
        return {"event_type": value.strip()}
    if isinstance(value, dict) and value.get("event_type"):
        return value
    raise _bad(f"`{name}`: an event name, or an object with `event_type` "
               "(plus optional `filters`, `group_by`).")


def _window(start: Optional[str], end: Optional[str], long_range: bool) -> tuple:
    from oto.tools.amplitude import day

    if not start or not end:
        raise _bad("`start` and `end` are required (YYYY-MM-DD).")
    try:
        s, e = day(start), day(end)
    except ValueError as err:
        raise _bad(str(err)) from None
    days = (date(int(e[:4]), int(e[4:6]), int(e[6:])) -
            date(int(s[:4]), int(s[4:6]), int(s[6:]))).days + 1
    if days < 1:
        raise _bad("`end` is before `start`.")
    if days > MAX_RANGE_DAYS:
        raise _bad(f"{days} days: the window is capped at {MAX_RANGE_DAYS}.")
    if days > LONG_RANGE_DAYS and not long_range:
        raise _bad(f"{days} days: windows over {LONG_RANGE_DAYS} days spend the "
                   "project's hourly query budget fast — pass `long_range=True` "
                   "if that is intended, or use a weekly/monthly `interval`.")
    return s, e


def _segments(segments: Optional[List[dict]]) -> Optional[List[dict]]:
    if segments and len(segments) > MAX_SEGMENTS:
        raise _bad(f"{len(segments)} segments: at most {MAX_SEGMENTS} per query "
                   "(each one multiplies the query's cost).")
    return segments or None


def _preview(text: str) -> dict:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return {"total_lines": len(lines), "lines": lines[:PREVIEW_LINES],
            "truncated": len(lines) > PREVIEW_LINES}


# ---------------------------------------------------------------------------
# Slim views
# ---------------------------------------------------------------------------

def _rows(res: Any) -> list:
    data = res.get("data") if isinstance(res, dict) else res
    return [r for r in (data or []) if isinstance(r, dict)]


def _slim_named(r: dict, key: str) -> dict:
    cat = r.get("category")
    out = {"name": r.get(key), "type": r.get("type"),
           "category": cat.get("name") if isinstance(cat, dict) else cat,
           "description": r.get("description") or None,
           "hidden": True if r.get("is_hidden") else None,
           "active": False if r.get("is_active") is False else None}
    return {k: v for k, v in out.items() if v is not None}


def _slim_cohort(row: dict) -> dict:
    out = {"id": row.get("id"), "name": row.get("name"), "size": row.get("size"),
           "description": row.get("description") or None,
           "last_computed": row.get("lastComputed"),
           "archived": True if row.get("archived") else None}
    return {k: v for k, v in out.items() if v is not None}


def _slim_activity_event(ev: dict) -> dict:
    out = {"event_type": ev.get("event_type"), "event_time": ev.get("event_time"),
           "session_id": ev.get("session_id"),
           "event_properties": ev.get("event_properties") or None}
    return {k: v for k, v in out.items() if v is not None}


def register(mcp: FastMCP) -> None:
    connector_verify.register("amplitude", _verify)

    @mcp.tool()
    def amplitude_schema(
        op: Literal["events", "event_properties", "user_properties",
                    "group_properties", "volumes"] = "events",
        event_type: Optional[str] = None,
        search: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """What this Amplitude project tracks — read it BEFORE writing a query:
        a guessed event name returns zeros, not an error.

        `op`: `events` (declared event types), `event_properties` (all, or of
        one `event_type`), `user_properties` (custom ones are prefixed `gp:` —
        use that exact name in segments and group-bys), `group_properties`
        (`grp:`, accounts; empty without the Accounts add-on), `volumes`
        (event types with their weekly totals — what actually fires).
        `search` filters names (case-insensitive). Returns `{op, count,
        items: [{name, type?, category?, description?}]}`; `full=True` the
        raw payload.

        Args:
            op: events | event_properties | user_properties | group_properties | volumes.
            event_type: event_properties — only this event's properties.
            search: substring filter on names.
            full: raw payload.
        """
        if op != "event_properties":
            _refuse_ignored(op, "only op='event_properties' takes it",
                            event_type=event_type)
        c = _client()
        if op == "events":
            res, key = _run(lambda: c.list_event_types()), "event_type"
        elif op == "event_properties":
            res, key = _run(lambda: c.list_event_properties(event_type)), "event_property"
        elif op == "user_properties":
            res, key = _run(lambda: c.list_user_properties()), "user_property"
        elif op == "group_properties":
            res, key = _run(lambda: c.list_group_properties()), "group_property"
        else:
            res, key = _run(lambda: c.list_events()), "value"
        if full:
            return res if isinstance(res, dict) else {"data": res}
        items = []
        for r in _rows(res):
            item = _slim_named(r, key)
            if op == "volumes":
                item["weekly_totals"] = r.get("totals")
            items.append(item)
        if search:
            needle = search.lower()
            items = [i for i in items if needle in str(i.get("name") or "").lower()]
        return {"op": op, "count": len(items), "items": items}

    @mcp.tool()
    def amplitude_chart(
        chart_id: str,
        format: Literal["json", "csv"] = "json",
    ) -> dict:
        """Run a SAVED Amplitude chart — the number the team reads in
        Amplitude, computed with the chart's own definition. Prefer this to
        rebuilding a funnel or retention with `amplitude_query`: conversion
        window, step order, exclusions and attribution do not survive being
        re-specified by hand, and the result silently disagrees with the
        team's dashboard.

        The chart id is the last segment of its URL
        (`app.amplitude.com/analytics/<org>/chart/<chart_id>`). `json` returns
        Amplitude's result object (`{data: {series, seriesLabels, xValues,
        ...}}`); if it is refused, `csv` returns the chart's export as a
        bounded preview `{total_lines, lines, truncated}`.

        Args:
            chart_id: the saved chart's id.
            format: json (default) | csv.
        """
        if format == "csv":
            return {"chart_id": chart_id,
                    **_preview(_run(lambda: _client().chart_csv(chart_id)))}
        return _run(lambda: _client().chart_query(chart_id))

    @mcp.tool()
    def amplitude_user(
        op: Literal["search", "activity"] = "search",
        user: Optional[str] = None,
        amplitude_id: Optional[str] = None,
        limit: int = 50,
        full: bool = False,
    ) -> dict:
        """One Amplitude user: find them, then read their event stream.
        Budget: 360 calls/hour for the project, shared by both ops.

        `search` (`user` = a user id, device id or Amplitude id) returns
        `{matches: [{amplitude_id, user_id, ...}]}`. `activity` needs the
        `amplitude_id` from `search` (not the user id) and returns the user's
        summary and latest events `[{event_type, event_time, session_id,
        event_properties}]`; `full=True` the raw payload.

        Args:
            op: search | activity.
            user: search — user id, device id or Amplitude id.
            amplitude_id: activity — from op='search'.
            limit: activity — events returned, 1-1000 (default 50).
            full: raw payload.
        """
        if op == "search":
            _refuse_ignored(op, "op='activity' takes an `amplitude_id`",
                            amplitude_id=amplitude_id)
            if not user:
                raise _bad("op='search' needs `user`.")
            return _run(lambda: _client().user_search(user))
        _refuse_ignored(op, "op='search' takes `user`", user=user)
        if not amplitude_id:
            raise _bad("op='activity' needs `amplitude_id` (from op='search').")
        limit = max(1, min(int(limit or 50), 1000))
        res = _run(lambda: _client().user_activity(amplitude_id, limit=limit))
        if full or not isinstance(res, dict):
            return res
        return {"user": res.get("userData"),
                "events": [_slim_activity_event(e) for e in res.get("events") or []
                           if isinstance(e, dict)][:limit]}

    @mcp.tool()
    def amplitude_cohort(
        op: Literal["list", "request", "members"] = "list",
        cohort_id: Optional[str] = None,
        request_id: Optional[str] = None,
        props: bool = False,
    ) -> dict:
        """Amplitude behavioral cohorts (Growth / Enterprise add-on; a
        download counts against 500 per month).

        `list` — the project's cohorts `[{id, name, size, last_computed}]`.
        `request` (`cohort_id`) starts a download job and waits up to ~20 s:
        returns `{request_id, status}`; when not finished, call `members`
        with that `request_id` later. `members` (`request_id`) returns
        `{status}` while running, then a bounded preview of the members
        `{total_lines, lines, truncated}` (CSV, header first).

        Args:
            op: list | request | members.
            cohort_id: request — the cohort id (from op='list').
            request_id: members — from op='request'.
            props: request — include user properties in the file.
        """
        if op == "list":
            _refuse_ignored(op, "use op='request'", cohort_id=cohort_id,
                            request_id=request_id)
            res = _run(lambda: _client().list_cohorts())
            rows = (res.get("cohorts") if isinstance(res, dict) else res) or []
            return {"cohorts": [_slim_cohort(r) for r in rows if isinstance(r, dict)]}
        c = _client()
        if op == "request":
            _refuse_ignored(op, "op='members' takes it", request_id=request_id)
            if not cohort_id:
                raise _bad("op='request' needs `cohort_id` (from op='list').")
            job = _run(lambda: c.request_cohort(cohort_id, props=props))
            request_id = (job or {}).get("request_id")
            if not request_id:
                return {"status": "unknown", "response": job}
            deadline = time.monotonic() + COHORT_WAIT_S
            while time.monotonic() < deadline:
                status = _run(lambda: c.cohort_status(request_id))
                if "COMPLETED" in str((status or {}).get("async_status", "")).upper():
                    return {"request_id": request_id, "status": "completed",
                            **_preview(_run(lambda: c.cohort_file(request_id)))}
                time.sleep(2)
            return {"request_id": request_id, "status": "running",
                    "next": "call op='members' with this request_id in a minute"}
        _refuse_ignored(op, "op='request' takes it", cohort_id=cohort_id)
        if not request_id:
            raise _bad("op='members' needs `request_id` (from op='request').")
        status = _run(lambda: c.cohort_status(request_id))
        if "COMPLETED" not in str((status or {}).get("async_status", "")).upper():
            return {"request_id": request_id, "status": "running",
                    "async_status": (status or {}).get("async_status")}
        return {"request_id": request_id, "status": "completed",
                **_preview(_run(lambda: c.cohort_file(request_id)))}
