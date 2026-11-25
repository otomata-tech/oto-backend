"""PostHog — product analytics: HogQL, events, persons, accounts, insights,
feature flags, session recordings.

Wraps `oto.tools.posthog.client.PostHogClient`. Credential with THREE fields
(`secret_kind="fields"`, resolved by `access.resolve_credential_fields`):

- `api_key` — the **personal** key `phx_…`. ⚠️ The PROJECT key `phc_…`, the one
  PostHog puts forward most, is refused by the read API; the client
  rejects it at construction with a message saying where to get the right one.
- `host` — NOT secret: `https://us.posthog.com` (default) or
  `https://eu.posthog.com`, or a self-hosted instance. The region is part
  of the address; a US key is unknown on the EU side (the symptom is a 401, not a
  region message).
- `project_id` — NOT secret, optional: pins the key to ONE project. Omitted, the
  project is discovered from the key.

**byo-only**: this is the customer's product data, there is no shared oto key.

**Eight tools**, verb in `op=` (ADR 0047). No parameter is silently
dropped (`_refuse_ignored`).

**Nothing changes the product.** Creating or toggling a feature flag, writing an
insight, deleting a person or a recording do not exist on the
client — not merely "not exposed here". Toggling a flag modifies the product
for real users; deleting a person is irreversible and
regulated. The ONLY write is the annotation, purely additive.

⚠️ **The raw `/query/` response is 93% noise**: measured at 2,525
characters for a two-cell result, of which 1,156 are `modifiers` and 510
generated ClickHouse SQL. `_projeter` reduces it to the useful fields (175 chars on
the same call) — without which every query would eat the response budget
(see `docs/conventions.md`).

**Live-tested on 2026-08-22** against a real PostHog Cloud US project
(real customer organization): identity, project discovery, HogQL,
typed queries, re-running a saved insight, schema (156 tables,
`events` at 52 columns), the 14 resource families and annotation
writing respond as coded. `groups_types` returns a BARE list (not
the `{results}` envelope), and `/events/` like `/persons/` carry NO
`count` — three shapes that cannot be deduced from the docs.
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, egress
from ..connectors import verify as connector_verify


def _check_host(host) -> None:
    """Egress guard on the PostHog host, when set.

    Empty = the SaaS, a constant of the lib. Filled in, it designates a
    self-hosted instance — hence potentially a host on the platform's
    internal network (`oto_mcp/egress.py`)."""
    valeur = (host or "").strip()
    if valeur:
        egress.check_url(valeur, connector="posthog", field="host")


_DEFAULT_LIMIT = 100

# The fields of the `/query/` response that carry information; everything else
# (modifiers, clickhouse, cache_key, timings…) is internal diagnostics.
_QUERY_KEEP = ("columns", "types", "results", "hasMore", "hogql", "error")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, hint: str, **provided) -> None:
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op={op!r} does not use `{name}` — {hint}")


def _projeter(reponse: Any) -> Any:
    """Reduce a `/query/` response to the useful fields (see the module docstring)."""
    if not isinstance(reponse, dict):
        return reponse
    out = {k: reponse[k] for k in _QUERY_KEEP if k in reponse and reponse[k] is not None}
    if reponse.get("hasMore"):
        out["note"] = ("Result TRUNCATED by PostHog. Add an explicit LIMIT, "
                       "aggregate in the query, or narrow the window.")
    return out or reponse


def _upstream_message(e) -> str:
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = body.get("detail") or body.get("error") or ""
    code = body.get("code")
    if code == "hogql_query_error" or status == 400:
        # PostHog's message names the offending field and its position: it is more
        # useful to the agent than any rewording.
        errs = ((body.get("extra") or {}).get("hogql_metadata") or {}).get("errors") or []
        pos = f" (position {errs[0].get('start')}-{errs[0].get('end')})" if errs else ""
        return (f"PostHog refused the query{pos}: {detail} — check the table and "
                f"column names with `posthog_schema`.")
    if status in (401, 403):
        return (f"PostHog rejected the key (HTTP {status}): {detail} — check (1) that "
                f"it is a PERSONAL `phx_…` key and not a project key "
                f"`phc_…`, (2) the configured REGION (us/eu — a key from one region is "
                f"unknown to the other), (3) the key's scopes.")
    if status == 404:
        return f"PostHog: resource not found — {detail}"
    if status == 429:
        return "PostHog: too many requests (429) — retry in a moment."
    if status >= 500:
        return f"PostHog is temporarily unavailable (HTTP {status}) — retry later."
    return f"PostHog refused the request (HTTP {status}): {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe: identity → project → query.

    A PostHog key carries SCOPES chosen at creation: a key without
    `query:read` authenticates perfectly then fails on the first real call.
    Settling for an identity GET would say "OK" where the flagship tool is
    unusable — hence the three steps, the last one really exercising `/query/`.
    """
    from oto.tools.posthog.client import PostHogClient
    cfg = config or {}
    _check_host(cfg.get("host"))
    client = PostHogClient(fields["api_key"], host=cfg.get("host") or None,
                           project_id=cfg.get("project_id") or None)
    client.current_user()
    project = client.resolve_project_id()
    try:
        client.query("SELECT 1")
    except Exception as e:  # noqa: BLE001 — the exception message IS the error return
        raise RuntimeError(
            f"The key authenticates fine and sees project {project}, but cannot "
            f"run a query — it is probably missing the `query:read` scope. "
            f"Detail: {e}") from e


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.posthog.client import PostHogClient

    connector_verify.register("posthog", _verify)

    def _client() -> PostHogClient:
        fields = access.resolve_credential_fields("posthog")
        _check_host(fields.get("host"))
        return PostHogClient(fields["api_key"], host=fields.get("host") or None,
                             project_id=fields.get("project_id") or None)

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # ================================================================
    # The query — free HogQL OR a named type
    # ================================================================

    @mcp.tool()
    def posthog_query(
        hogql: Optional[str] = None,
        query: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Run an analytics query — free HogQL, or one of PostHog's named query
        kinds. Pass exactly one of `hogql` or `query`.

        ⚠️ **Use `query` (`FunnelsQuery`, `RetentionQuery`) — not `hogql` — for
        funnels and retention.** PostHog's
        funnel semantics — ordered vs unordered steps, conversion window,
        exclusion steps, attribution — do not survive being rewritten as SQL.
        Hand-rolled HogQL returns a plausible number that disagrees with the
        number on the team's own dashboard, and nothing signals the error. Better
        still, when the team already built the chart, call
        `posthog_insight(op="run", insight_id=…)` instead of this tool.

        **HogQL dialect** — ClickHouse SQL with PostHog accessors. Get these
        wrong and the query returns a confidently wrong number:
        - properties: `properties.$browser`, `person.properties.email` — NOT
          `JSONExtract(...)`. Values are strings, so compare numbers with
          `toFloat(properties.amount) > 10`.
        - the event-name column is `event` (not `event_name`); time is
          `timestamp`, filtered like `timestamp >= now() - INTERVAL 7 DAY`.
        - unique users is `uniq(person_id)` — never
          `count(distinct distinct_id)`, which counts devices, not people.
        - joinable tables: `events`, `persons`, `sessions`, `groups`. Discover
          them and their columns with `posthog_schema` BEFORE writing a query.

        PostHog caps an unbounded query at 101 rows and sets `hasMore`; add your
        own `LIMIT`, or aggregate in the query rather than paginating.

        Args:
            hogql: a HogQL (SQL) string. Best for counts, breakdowns, joins and
                anything ad-hoc.
            query: a full PostHog query object with its own `kind` —
                `TrendsQuery`, `FunnelsQuery`, `RetentionQuery`,
                `StickinessQuery`, `LifecycleQuery`.
            project_id: target another project than the configured default.
        """
        if bool(hogql) == bool(query):
            raise _bad("Provide EXACTLY one of `hogql` (free SQL) or `query` (typed "
                       "object with its `kind`) — not both, not neither.")
        client = _client()
        if hogql:
            return _run(lambda: _projeter(client.query(hogql, project_id=project_id)))
        return _run(lambda: _projeter(client.run_query(query, project_id=project_id)))

    # ================================================================
    # The project's vocabulary — without which no query can be written
    # ================================================================

    @mcp.tool()
    def posthog_schema(
        op: Literal["tables", "columns", "events", "properties", "values"] = "tables",
        table: Optional[str] = None,
        property_key: Optional[str] = None,
        search: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """What this project actually contains — read this BEFORE writing HogQL.

        "tables" returns names only, and "columns" one table at a time, on
        purpose: the full schema is ~156 tables and `events` alone has 52
        columns (measured), which would swamp the response budget in one call.

        An empty "events" result means the project has no data yet — say so
        rather than writing a query that will correctly return zero.

        Args:
            op: "tables" (default, the queryable table names) | "columns" (one
                table's columns — REQUIRES `table`) | "events" (event types seen
                in this project) | "properties" (known properties) | "values"
                (observed values of one property — REQUIRES `property_key`).
            table: REQUIRED by "columns", e.g. "events", "persons", "sessions".
            property_key: REQUIRED by "values".
            search: filter for "events"/"properties".
            project_id: target another project.

        """
        client = _client()
        if op == "tables":
            _refuse_ignored(op, "use op='columns' for the columns of ONE table",
                            table=table, property_key=property_key, search=search)

            def _tables():
                schema = client.database_schema(project_id=project_id)
                tables = schema.get("tables") or {}
                return {"tables": sorted(tables),
                        "count": len(tables),
                        "note": "A table's columns: op='columns', table='events'."}
            return _run(_tables)
        if op == "columns":
            if not table:
                raise _bad("op='columns' requires `table` (e.g. 'events', 'persons')")
            _refuse_ignored(op, "these fields do not apply to op='columns'",
                            property_key=property_key, search=search)

            def _columns():
                schema = client.database_schema(project_id=project_id)
                tables = schema.get("tables") or {}
                entry = tables.get(table)
                if entry is None:
                    raise _bad(f"Table {table!r} is unknown to this project — list them with "
                               "op='tables'.")
                fields = entry.get("fields") or {}
                return {"table": table,
                        "columns": {n: f.get("type") for n, f in fields.items()},
                        "count": len(fields)}
            return _run(_columns)
        if op == "events":
            _refuse_ignored(op, "these fields do not apply to op='events'",
                            table=table, property_key=property_key)
            return _run(lambda: client.list_event_definitions(
                project_id=project_id, search=search, limit=_DEFAULT_LIMIT))
        if op == "properties":
            _refuse_ignored(op, "these fields do not apply to op='properties'",
                            table=table, property_key=property_key)
            return _run(lambda: client.list_property_definitions(
                project_id=project_id, search=search, limit=_DEFAULT_LIMIT))
        if op == "values":
            if not property_key:
                raise _bad("op='values' requires `property_key`")
            _refuse_ignored(op, "these fields do not apply to op='values'",
                            table=table, search=search)
            return _run(lambda: client.list_property_values(
                property_key, project_id=project_id))
        raise _bad("op must be 'tables', 'columns', 'events', 'properties' or 'values'")

    # ================================================================
    # Persons & cohorts
    # ================================================================

    @mcp.tool()
    def posthog_person(
        op: Literal["list", "get", "activity", "cohorts", "cohort_persons"] = "list",
        person_id: Optional[str] = None,
        cohort_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """People tracked in the product, and the cohorts that segment them.

        ⚠️ `/persons/` returns no `count` (measured) — do not report a total
        from a page. To COUNT or aggregate people, use
        `posthog_query(hogql="SELECT uniq(person_id) FROM events WHERE …")`;
        paginating raw persons is slow and gives a wrong total.

        For B2B questions about ACCOUNTS rather than individuals, use
        `posthog_group` — persons cannot answer "which customers are churning".

        Args:
            op: "list" (default) | "get" | "activity" | "cohorts" |
                "cohort_persons".
            person_id: REQUIRED by "get"/"activity".
            cohort_id: REQUIRED by "cohort_persons"; filters "list".
            search: "list" — matches email, name or distinct_id.
            limit: page size (default 100). project_id: another project.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific person",
                            person_id=person_id)
            return _run(lambda: client.list_persons(
                project_id=project_id, search=search, cohort=cohort_id,
                limit=limit or _DEFAULT_LIMIT))
        if op == "cohorts":
            _refuse_ignored(op, "the cohort list does not take these filters",
                            person_id=person_id, cohort_id=cohort_id, search=search)
            return _run(lambda: client.list_cohorts(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "cohort_persons":
            if not cohort_id:
                raise _bad("op='cohort_persons' requires `cohort_id`")
            _refuse_ignored(op, "these fields do not apply to op='cohort_persons'",
                            person_id=person_id, search=search)
            return _run(lambda: client.list_cohort_persons(
                cohort_id, project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op in ("get", "activity"):
            if not person_id:
                raise _bad(f"op={op!r} requires `person_id`")
            _refuse_ignored(op, "these filters only apply to op='list'",
                            cohort_id=cohort_id, search=search)
            if op == "get":
                return _run(lambda: client.get_person(person_id, project_id=project_id))
            return _run(lambda: client.list_person_activity(
                person_id, project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        raise _bad("unknown op for posthog_person")

    # ================================================================
    # Groups — analytics per ACCOUNT (B2B)
    # ================================================================

    @mcp.tool()
    def posthog_group(
        op: Literal["types", "list", "find"] = "types",
        group_type_index: Optional[int] = None,
        group_key: Optional[str] = None,
        search: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Group analytics — the ACCOUNT-level view (companies, workspaces),
        as opposed to individual people.

        Call op="types" first: without the index no group call is possible. An
        EMPTY result means this project does not do group analytics at all, in
        which case account-level questions ("which customers are churning") have
        no answer here — say so rather than silently answering per person.

        `groups` is also a joinable HogQL table, so once you know the index,
        `posthog_query` can aggregate by account.

        Args:
            op: "types" (default) | "list" | "find".
            group_type_index: REQUIRED by "list"/"find" — from op="types".
            group_key: REQUIRED by "find" — the business key of one group.
            search: "list" filter. project_id: another project.

        """
        client = _client()
        if op == "types":
            _refuse_ignored(op, "the type list does not take these filters",
                            group_type_index=group_type_index, group_key=group_key,
                            search=search)

            def _types():
                types = client.list_group_types(project_id=project_id)
                if not types:
                    return {"group_types": [],
                            "note": ("This project does not do group analytics: there is "
                                     "no ACCOUNT level here, only persons.")}
                return {"group_types": types}
            return _run(_types)
        if group_type_index is None:
            raise _bad(f"op={op!r} requires `group_type_index` — read it with op='types'")
        if op == "list":
            _refuse_ignored(op, "use op='find' for a specific group", group_key=group_key)
            return _run(lambda: client.list_groups(
                group_type_index, project_id=project_id, search=search))
        if op == "find":
            if not group_key:
                raise _bad("op='find' requires `group_key`")
            _refuse_ignored(op, "op='find' targets a specific key", search=search)
            return _run(lambda: client.find_group(
                group_type_index, group_key, project_id=project_id))
        raise _bad("op must be 'types', 'list' or 'find'")

    # ================================================================
    # Insights & dashboards — the work the team has already done
    # ================================================================

    @mcp.tool()
    def posthog_insight(
        op: Literal["list", "get", "run", "dashboards", "dashboard"] = "list",
        insight_id: Optional[str] = None,
        dashboard_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Saved insights and dashboards — the charts the team already built.

        **`op="run"` is the highest-trust answer in this connector.** It replays
        the insight's OWN query definition through PostHog's planner, so the
        number matches what the team sees in the UI — with an optional different
        date window. Always prefer it to rebuilding a funnel or retention chart
        in HogQL, which produces a plausible number that quietly disagrees.

        Insights saved in PostHog's older `filters` format cannot be replayed
        this way; "run" says so explicitly instead of failing obscurely.

        Args:
            op: "list" (default) | "get" | "run" | "dashboards" | "dashboard".
            insight_id: REQUIRED by "get"/"run".
            dashboard_id: REQUIRED by "dashboard".
            date_from/date_to: "run" only — override the saved window, in
                PostHog syntax (`-7d`, `-30d`, `mStart`, `yStart`, or an ISO
                date). Omitted = the window saved with the insight.
            search/limit: "list"/"dashboards". project_id: another project.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific insight",
                            insight_id=insight_id, dashboard_id=dashboard_id,
                            date_from=date_from, date_to=date_to)
            return _run(lambda: client.list_insights(
                project_id=project_id, search=search, limit=limit or _DEFAULT_LIMIT))
        if op == "dashboards":
            _refuse_ignored(op, "the dashboard list does not take these filters",
                            insight_id=insight_id, dashboard_id=dashboard_id,
                            date_from=date_from, date_to=date_to)
            return _run(lambda: client.list_dashboards(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "dashboard":
            if not dashboard_id:
                raise _bad("op='dashboard' requires `dashboard_id`")
            _refuse_ignored(op, "these fields do not apply to op='dashboard'",
                            insight_id=insight_id, date_from=date_from,
                            date_to=date_to, search=search)
            return _run(lambda: client.get_dashboard(dashboard_id, project_id=project_id))
        if op in ("get", "run"):
            if not insight_id:
                raise _bad(f"op={op!r} requires `insight_id`")
            _refuse_ignored(op, "these fields do not apply here",
                            dashboard_id=dashboard_id, search=search)
            if op == "get":
                _refuse_ignored(op, "the window can only be replaced at execution (op='run')",
                                date_from=date_from, date_to=date_to)
                return _run(lambda: client.get_insight(insight_id, project_id=project_id))
            return _run(lambda: _projeter(client.run_insight(
                insight_id, date_from=date_from, date_to=date_to,
                project_id=project_id)))
        raise _bad("unknown op for posthog_insight")

    # ================================================================
    # Feature flags & experiments — READ
    # ================================================================

    @mcp.tool()
    def posthog_flag(
        op: Literal["list", "get", "experiments", "experiment"] = "list",
        flag_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Feature flags and experiments — read-only.

        Each flag carries its `key`, whether it is `active`, and its rollout
        conditions. Creating, editing or toggling a flag is deliberately not
        available: a toggle ships to real users, and that is not a decision an
        assistant should be able to take mid-conversation. Do it in PostHog.

        Args:
            op: "list" (default) | "get" | "experiments" | "experiment".
            flag_id: REQUIRED by "get". experiment_id: REQUIRED by "experiment".
            limit, project_id: as elsewhere.
            offset: "list"/"experiments" — skip that many items, to read the next
                page (PostHog's `next` link carries the next offset). Default 0.

        """
        client = _client()
        if offset is not None and offset < 0:
            raise _bad("`offset` must be 0 or more")
        page = {"offset": offset} if offset else {}
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific flag",
                            flag_id=flag_id, experiment_id=experiment_id)
            return _run(lambda: client.list_feature_flags(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT, **page))
        if op == "experiments":
            _refuse_ignored(op, "the experiment list does not take these filters",
                            flag_id=flag_id, experiment_id=experiment_id)
            return _run(lambda: client.list_experiments(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT, **page))
        if op == "get":
            if not flag_id:
                raise _bad("op='get' requires `flag_id`")
            _refuse_ignored(op, "op='get' targets a flag", experiment_id=experiment_id,
                            offset=offset)
            return _run(lambda: client.get_feature_flag(flag_id, project_id=project_id))
        if op == "experiment":
            if not experiment_id:
                raise _bad("op='experiment' requires `experiment_id`")
            _refuse_ignored(op, "op='experiment' targets an experiment", flag_id=flag_id,
                            offset=offset)
            return _run(lambda: client.get_experiment(experiment_id, project_id=project_id))
        raise _bad("op must be 'list', 'get', 'experiments' or 'experiment'")

    # ================================================================
    # Session recordings
    # ================================================================

    @mcp.tool()
    def posthog_recording(
        op: Literal["list", "get"] = "list",
        recording_id: Optional[str] = None,
        person_uuid: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """Session recordings — metadata only, never the video.

        Returns duration, the person, and pages visited — enough to decide which
        session a human should watch, and to hand back its id. ⚠️ No `count` in
        the envelope (measured).

        Args:
            op: "list" (default) | "get".
            recording_id: REQUIRED by "get".
            person_uuid: "list" filter — one person's sessions.
            date_from/date_to: "list" window (`-7d` or ISO date).
            limit, project_id: as elsewhere.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific recording",
                            recording_id=recording_id)
            return _run(lambda: client.list_session_recordings(
                project_id=project_id, person_uuid=person_uuid, date_from=date_from,
                date_to=date_to, limit=limit or _DEFAULT_LIMIT))
        if op == "get":
            if not recording_id:
                raise _bad("op='get' requires `recording_id`")
            _refuse_ignored(op, "these filters only apply to op='list'",
                            person_uuid=person_uuid, date_from=date_from, date_to=date_to)
            return _run(lambda: client.get_session_recording(
                recording_id, project_id=project_id))
        raise _bad("op must be 'list' or 'get'")

    # ================================================================
    # Project & annotations — including the only write
    # ================================================================

    @mcp.tool()
    def posthog_project(
        op: Literal["current", "list", "annotations", "annotate"] = "current",
        content: Optional[str] = None,
        date_marker: Optional[str] = None,
        limit: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> object:
        """The project this key operates on, and the annotations on its charts.

        Call op="current" when a number looks surprising: it confirms WHICH
        project and WHICH account answered, which is the usual explanation.

        **"annotate" is the only write in this connector.** It drops a dated
        marker on the project's charts ("v2.3 shipped here") — purely additive,
        it changes no measurement, only how it reads.

        Args:
            op: "current" (default — which project and which account) | "list"
                (every project the key can read) | "annotations" | "annotate".
            content: REQUIRED by "annotate" — the marker's text.
            date_marker: "annotate" — the instant marked (ISO 8601).
                Default: now.
            limit, project_id: as elsewhere.

        """
        client = _client()
        if op == "current":
            _refuse_ignored(op, "op='current' does not take these fields",
                            content=content, date_marker=date_marker)

            def _current():
                me = client.current_user()
                org = me.get("organization") or {}
                return {"project_id": client.resolve_project_id(),
                        "organization": org.get("name"),
                        "account": me.get("email"),
                        "host": client.host}
            return _run(_current)
        if op == "list":
            _refuse_ignored(op, "op='list' does not take these fields",
                            content=content, date_marker=date_marker)
            return _run(lambda: client.list_projects())
        if op == "annotations":
            _refuse_ignored(op, "use op='annotate' to add one",
                            content=content, date_marker=date_marker)
            return _run(lambda: client.list_annotations(
                project_id=project_id, limit=limit or _DEFAULT_LIMIT))
        if op == "annotate":
            if not content:
                raise _bad("op='annotate' requires `content`")
            return _run(lambda: client.create_annotation(
                content, date_marker=date_marker, project_id=project_id))
        raise _bad("op must be 'current', 'list', 'annotations' or 'annotate'")
