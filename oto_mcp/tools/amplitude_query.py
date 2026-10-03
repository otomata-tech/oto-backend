"""Amplitude — the Dashboard REST query tool (`amplitude_query`).

Second module of the connector (`amplitude` holds the schema, charts, users,
cohorts, the credential and the probe): it only carries the query, whose
arguments differ by `op`. Each op refuses the arguments it does not use, the
window is cost-guarded (`amplitude._window`) and events can be passed by name.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

from fastmcp import FastMCP

from . import amplitude as A

if TYPE_CHECKING:  # `_client()` return annotation only — never evaluated
    from oto.tools.amplitude import AmplitudeClient

_INTERVALS = {"hour": -3600000, "day": 1, "week": 7, "month": 30}


def _client() -> AmplitudeClient:
    """Same factory as `amplitude._client` (declared here for the version probe)."""
    return A._client()


def _slim_result(res: Any) -> dict:
    """The raw answer wraps `data` in ~2 KB of engine diagnostics; only `data` and
    the query's cost against the hourly budget are kept."""
    if not (isinstance(res, dict) and "data" in res):
        return {"data": res}
    out = {"data": res["data"]}
    if res.get("novaCost") is not None:
        out["cost"] = res["novaCost"]
    return out


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def amplitude_query(
        op: Literal["segmentation", "funnel", "retention", "active_users", "sessions"],
        start: str,
        end: str,
        event: Optional[A.EventArg] = None,
        events: Optional[List[A.EventArg]] = None,
        return_event: Optional[A.EventArg] = None,
        metric: Optional[str] = None,
        interval: Optional[Literal["hour", "day", "week", "month"]] = None,
        segments: Optional[List[Dict[str, Any]]] = None,
        group_by: Optional[str] = None,
        limit: Optional[int] = None,
        mode: Optional[str] = None,
        conversion_window_days: Optional[int] = None,
        session_kind: Optional[Literal["length", "average", "peruser"]] = None,
        long_range: bool = False,
    ) -> dict:
        """Compute an Amplitude metric. If the team already has the chart, use
        `amplitude_chart` instead — it returns THEIR number. Check event and
        property names with `amplitude_schema` first: a misspelt event returns
        zeros, not an error.

        `op`:
        - `segmentation` — `event` over time; `metric` uniques (default) |
          totals | pct_dau | average | sums.
        - `funnel` — `events` = the ordered steps (2-10); `mode` ordered
          (default) | unordered | sequential; `conversion_window_days`
          (default 30).
        - `retention` — users who did `event`, coming back to do
          `return_event` (`_active` = any event); `mode` n-day | rolling |
          bracket.
        - `active_users` — `metric` active (default) | new.
        - `sessions` — `session_kind` length | average | peruser.

        An event is a name (`"Sign Up"`) or `{"event_type": "Sign Up",
        "filters": [{"subprop_type": "event", "subprop_key": "plan",
        "subprop_op": "is", "subprop_value": ["pro"]}]}`. `segments` =
        `[{"prop": "country", "op": "is", "values": ["France"]}]` (≤5).
        `group_by` = one property (`country`, `gp:plan`). Windows over 92
        days need `long_range=True` (cost budget: 108k/hour per project).
        Returns `{data, cost}`: Amplitude's series, labels and x values, and what the query spent.

        Args:
            op: segmentation | funnel | retention | active_users | sessions.
            start: first day, YYYY-MM-DD.
            end: last day, YYYY-MM-DD (inclusive).
            event: segmentation, retention — the (starting) event.
            events: funnel — the ordered steps.
            return_event: retention — the return event.
            metric: segmentation, active_users — see above.
            interval: segmentation, retention, active_users — hour | day | week | month.
            segments: user segments to compare (≤5).
            group_by: one property to break down by.
            limit: segmentation, funnel — group-by values, ≤1000.
            mode: funnel, retention — see above.
            conversion_window_days: funnel — default 30.
            session_kind: sessions — length | average | peruser.
            long_range: allow a window over 92 days.
        """
        s, e = A._window(start, end, long_range)
        segs = A._segments(segments)
        i = _INTERVALS.get(interval) if interval else None

        if op == "segmentation":
            A._refuse_ignored(op, "segmentation takes one `event`", events=events,
                              return_event=return_event, mode=mode,
                              conversion_window_days=conversion_window_days,
                              session_kind=session_kind)
            ev = A._event(event, "event")
            res = A._run(lambda: _client().segmentation(
                ev, s, e, metric=metric, interval=i, segments=segs,
                group_by=group_by, limit=limit))
        elif op == "funnel":
            A._refuse_ignored(op, "a funnel takes its steps in `events`", event=event,
                              return_event=return_event, metric=metric,
                              interval=interval, session_kind=session_kind)
            steps = [A._event(x, "events[]") for x in (events or [])]
            if not 2 <= len(steps) <= A.MAX_FUNNEL_STEPS:
                raise A._bad(f"`events`: a funnel has 2 to {A.MAX_FUNNEL_STEPS} steps.")
            cs = conversion_window_days * 86400 if conversion_window_days else None
            res = A._run(lambda: _client().funnel(
                steps, s, e, mode=mode, conversion_window_seconds=cs,
                segments=segs, group_by=group_by, limit=limit))
        elif op == "retention":
            A._refuse_ignored(op, "retention takes `event` and `return_event`",
                              events=events, metric=metric, limit=limit,
                              conversion_window_days=conversion_window_days,
                              session_kind=session_kind)
            se = A._event(event, "event")
            re_ = A._event(return_event or "_active", "return_event")
            res = A._run(lambda: _client().retention(
                se, re_, s, e, mode=mode, interval=i, segments=segs,
                group_by=group_by))
        elif op == "active_users":
            A._refuse_ignored(op, "active_users counts users, not events",
                              event=event, events=events, return_event=return_event,
                              limit=limit, mode=mode,
                              conversion_window_days=conversion_window_days,
                              session_kind=session_kind)
            res = A._run(lambda: _client().active_users(
                s, e, metric=metric, interval=i, segments=segs, group_by=group_by))
        else:
            A._refuse_ignored(op, "sessions only takes `session_kind` and the window",
                              event=event, events=events, return_event=return_event,
                              metric=metric, interval=interval, segments=segments,
                              group_by=group_by, limit=limit, mode=mode,
                              conversion_window_days=conversion_window_days)
            kind = session_kind or "average"
            res = A._run(lambda: _client().sessions(kind, s, e))

        return {"op": op, "start": s, "end": e, **_slim_result(res)}
