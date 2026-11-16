"""Origami — email + LinkedIn campaigns: lead tables, campaigns, launch, stats.

Wraps `oto.tools.origami.client.OrigamiClient` (API v2 `origami.chat/api/v2`, Bearer
`og_live_…`). keyed `api_key`, byo-only (no platform key): the org connects ITS
Origami account — enrichment credits and sends are its own.

⚠️ **DESIGN NOTE — first third-party integration whose write SENDS.** oto's
"generic" HTTP integrations are designed read-only (the `api` connector announces itself as
GET-only in the catalog); connectors that write (folk, lemlist, notion…) write
into a CRM or a tool, not toward third parties. Origami's value is in the POST:
create a table from a CSV, upsert rows, have the Origami agent draft a campaign,
and **launch it** — which sends emails and LinkedIn messages
to real people, at scale, with no way back. It is therefore oto's first
third-party connector whose write leaves the platform toward strangers; the
maintainer decides whether that is acceptable. Everything is implemented cleanly and EVERY mutating
tool is gated by the oto-wide `dry_run` convention (validation runs, the final
call is skipped, the response carries `dry_run: true` + a preview); launching is
`dry_run=True` BY DEFAULT — you must pass `dry_run=False` to send.

API facts verified live on 16–17/08/2026 and carried by the tool
docstrings (the agent must read them, there is nowhere else):
- lists in an envelope `{items[], nextCursor}` (50/page) — `origami_rows` follows
  `nextCursor` server-side up to `max_pages`;
- upsert only accepts `input` columns, addressed by their SLUG (dashes);
  unknown slug → 400 `UNKNOWN_FIELDS` — we read the columns BEFORE writing;
- upload is JSON (base64), never multipart; a CSV with `mode: "table"` CREATES
  a table;
- the campaign is created by an AGENTIC run (202 `{agent, run}`, to follow via
  `origami_run_get`); no global `GET /campaigns` (list by table) and no
  `GET /runs/{id}` (the run is read under its agent); `GET /sequences?workspaceId=`
  is the only view that sees all the sequences of a workspace;
- `blockPriorContacts=True` removes from the campaign anyone ALREADY enrolled
  before, EVEN in a deleted draft that was never sent;
- launch can answer 200 with `launch.blocked.missingChannels`: no sender
  account for those channels, NOTHING went out;
- deletion is two-step, and the 2nd step can answer 200 without deleting:
  we re-GET and only claim "deleted" on a 404.

Client calls are written in plain sight (`c.list_tables(…)`): that is what makes them
verifiable by the version-skew probe (`test_tools_client_methods_exist`).
"""
from __future__ import annotations

import base64
import csv
import io
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INVALID_REQUEST

from .. import access
from ..connectors import verify as connector_verify

# API cap per upsert call (beyond: 400).
_UPSERT_MAX_ROWS = 100
# Maximum pages followed by `origami_rows(op="list")` — 50 rows/page ⇒ 20 pages =
# 1,000 rows, a reasonable size for a tool return; beyond that, the agent calls again
# with the returned `cursor`.
_ROWS_MAX_PAGES_CAP = 20
# Rows shown in the preview of a CSV upload in dry_run.
_CSV_PREVIEW_ROWS = 5


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status, body = e.status_code, e.body
    code = body.get("code") if isinstance(body, dict) else None
    detail = body.get("error") if isinstance(body, dict) else body
    if status in (401, 403):
        return (f"Origami rejected the API key (HTTP {status}, {code}) — check the "
                "`og_live_…` key configured on this connector (Origami: Settings → API keys).")
    if status == 402:
        return (f"Origami: insufficient credits or plan (402, {code}) — {detail}. "
                "Top up the account, or reduce the scope (enrich=False, fewer rows).")
    if status == 404:
        return f"Origami: resource not found (404, {code}) — check the id. {detail}"
    if status == 400 and code == "UNKNOWN_FIELDS":
        return (f"Origami refused some row keys (400 UNKNOWN_FIELDS): {detail} — "
                "the keys of `rows` and `match_columns` are the SLUGS of the input columns "
                "(`origami_tables(op='columns')` → items[].slug, with dashes), never "
                f"the displayed names. Details: {body.get('details') if isinstance(body, dict) else ''}")
    if status == 409:
        return f"Origami: conflict (409, {code}) — {detail}"
    if status == 429:
        return (f"Origami: limit reached (429, {code}) — {detail}. Try again in a "
                "moment (100 req/min per org; concurrent agent runs are capped "
                "by the plan).")
    if status in (500, 502, 503, 504):
        return f"Origami is temporarily unavailable (HTTP {status}) — try again later."
    return f"Origami refused the request (HTTP {status}, {code}): {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test the connection" probe: list the workspaces (read, no side effects)."""
    from oto.tools.origami.client import OrigamiClient
    OrigamiClient(api_key=fields["key"]).list_workspaces(limit=1)


def _items(envelope: Any) -> list:
    """The `items` of a v2 list envelope (tolerant: a bare list passes too)."""
    if isinstance(envelope, dict):
        items = envelope.get("items")
        return items if isinstance(items, list) else []
    return envelope if isinstance(envelope, list) else []


def _column_slugs(columns_envelope: Any) -> tuple[set[str], set[str]]:
    """(INPUT slugs, all slugs) read from a `GET /tables/{id}/columns`."""
    inputs, all_slugs = set(), set()
    for col in _items(columns_envelope):
        if not isinstance(col, dict) or not col.get("slug"):
            continue
        all_slugs.add(col["slug"])
        if str(col.get("kind", "input")).lower() == "input":
            inputs.add(col["slug"])
    return inputs, all_slugs


def _parse_csv_preview(csv_text: str) -> dict:
    """Header + first N rows + total count, for the preview of an upload."""
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader, None)
    if not header or not any(h.strip() for h in header):
        raise _bad("`csv_text` has no header row: the first line must carry "
                   "the column names.")
    preview, count = [], 0
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        count += 1
        if len(preview) < _CSV_PREVIEW_ROWS:
            preview.append(dict(zip(header, row)))
    return {"columns": header, "rows": count, "preview": preview}


def _refuse_si_rien_n_a_ete_fait(res: dict) -> dict:
    """A COMPLETED run that produced NO action is REFUSED (#627, oto#175).

    Measured on 31/08: an incremental enrolment returned text claiming
    "19/19 people added, opening kept word for word", with an EMPTY action
    list. Then on 09/09/2026 (signal 830): three campaign creations in a row
    on the same table, each "completed" in ~90 s with `actions: []`, each
    answering in confident prose "Created the draft campaign … People
    enrolled: 15" and naming a campaign and a slug that do not exist —
    `origami_campaigns(op='list_for_table')` returned EMPTY after all three.
    The `aucune_action` flag added on 03/09 was the only discriminator across
    seven calls; but a flag either gets read or it doesn't, and the prose
    still travelled.

    ⚠️ **The worst possible combination for an unattended agent**: success
    prose and an empty trace. The prose comes from the model on the other
    side and we don't control it; what we do control is that it is NOT served
    as a success: the run is returned as an ERROR, which names the cause (no
    action, the announced campaign does not exist) and the remedy (check the
    table, retry the creation). Decision by Alexis on 12/09/2026.

    ⚠️ **The guard stays silent about what it cannot see.** The action list is
    not in the provider's documented contract: we look for it at the root,
    then under `response`, and we refuse ONLY if we found it and it is empty.
    If it is absent, we say nothing — a guard that guesses a shape produces
    false alarms, which costs the trust it is meant to serve.
    """
    if not isinstance(res, dict):
        return res
    statut = str(res.get("status") or "")
    if not statut or statut == "running":
        return res
    for source in (res, res.get("response")):
        if not isinstance(source, dict) or "actions" not in source:
            continue
        actions = source.get("actions")
        if isinstance(actions, list) and not actions:
            etapes = res.get("steps")
            faites = (etapes.get("completed") if isinstance(etapes, dict) else None)
            raise McpError(ErrorData(
                code=INVALID_REQUEST,
                message=(
                    f"Origami run `{res.get('id') or '?'}` finished (`{statut}`) WITH "
                    "NO ACTION: it created and changed nothing, whatever its "
                    "prose says — the campaign and slug it names do not exist, and "
                    "no person was enrolled. Do not report anything from this run "
                    "as done. Remedy: check the real state with "
                    "origami_campaigns(op='list_for_table', table_id=…) — if there is "
                    "no campaign, retry origami_campaign_create (same table, same "
                    "brief), ONCE: ⚠️ a run refused this way may have left a "
                    "\"Ready to launch\" draft in the Origami interface that the API "
                    "does not see, and every retry adds one; tell the human, who "
                    "checks the campaign list in Origami and deletes the "
                    "duplicates, and do not retry in a loop (during a provider "
                    "outage, eight retries in a row created nothing). If the brief "
                    "targeted an existing campaign, read it with "
                    "op='get' and op='people' (a number of people found that climbs "
                    "without the contacted count following signals sequences with no "
                    "recipient). The defect is on the provider's side: its run "
                    "ends \"completed\" instead of \"in error\"."),
                data={"aucune_action": True, "run_id": res.get("id"), "status": statut,
                      "steps_completed": faites}))
        break
    return res


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.origami.client import OrigamiClient

    connector_verify.register("origami", _verify)

    def _client() -> OrigamiClient:
        key, _ = access.resolve_api_key("origami")
        return OrigamiClient(api_key=key)

    def _run(fn):
        """Traduit un refus d'Origami en erreur d'outil actionnable."""
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # --- workspaces ---------------------------------------------------------

    @mcp.tool()
    def origami_workspaces(
        op: Literal["list", "create"] = "list",
        name: Optional[str] = None,
        search: Optional[str] = None,
        cursor: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Origami workspaces — the containers of tables, documents and campaigns.

        `op`:
        - **"list"** (default): `{items: [{id, name, url, createdAt}], nextCursor}`.
          `search` filters by name substring; pass `nextCursor` back as `cursor`.
        - **"create"**: creates a workspace named `name` (WRITE — needed before an
          upload-first flow). `dry_run=True` validates and returns
          `{dry_run: true, would_create: {name}}` without creating.

        Args:
            op: "list" | "create".
            name: op="create" — workspace name (≤ 80 chars).
            search: op="list" — case-insensitive substring on the name.
            cursor: op="list" — `nextCursor` of the previous page.
            dry_run: op="create" — preview only, nothing written.
        """
        if op == "list":
            return _run(lambda: _client().list_workspaces(cursor=cursor, search=search))
        if op == "create":
            if not name or not name.strip():
                raise _bad("op='create': `name` is required.")
            if len(name) > 80:
                raise _bad("op='create': `name` ≤ 80 characters.")
            if dry_run:
                return {"dry_run": True, "would_create": {"name": name.strip()}}
            return _run(lambda: _client().create_workspace(name.strip()))
        raise _bad(f"Invalid `op`: {op!r} (expected: list | create).")

    # --- tables -------------------------------------------------------------

    @mcp.tool()
    def origami_tables(
        op: Literal["list", "get", "columns"] = "list",
        workspace_id: Optional[str] = None,
        table_id: Optional[str] = None,
        cursor: Optional[str] = None,
        include_stats: bool = False,
    ) -> dict:
        """Origami tables (lead lists) — list them, read one, or read its columns.

        `op`:
        - **"list"** (default): every table of the org, or of `workspace_id` if given
          — `{items: [{id, workspaceId, name, leadCount, columns, credits, url}],
          nextCursor}` (50 per page; pass `nextCursor` back as `cursor`).
        - **"get"**: one table by `table_id` — name, leadCount, columns, credits
          consumed (`credits.lifetimeUsed`); `include_stats=True` adds the economics
          block (creditsPerLead, qualification, funnel).
        - **"columns"**: `{items: [{id, name, slug, kind, autoTrigger}]}` — READ THIS
          BEFORE ANY UPSERT: row keys and `match_columns` are the `slug` values
          (hyphenated, e.g. `first-name`) of `kind == "input"` columns, never the
          display names. Enrichment / score / sequence columns are not writable.

        Args:
            op: "list" | "get" | "columns".
            workspace_id: op="list" — scope to one workspace.
            table_id: op="get" / op="columns" — the table.
            cursor: op="list" — `nextCursor` of the previous page.
            include_stats: op="get" — attach the economics block.
        """
        if op == "list":
            return _run(lambda: _client().list_tables(workspace_id=workspace_id, cursor=cursor))
        if op not in ("get", "columns"):
            raise _bad(f"Invalid `op`: {op!r} (expected: list | get | columns).")
        if not table_id:
            raise _bad(f"op='{op}': `table_id` is required.")
        if op == "get":
            return _run(lambda: _client().get_table(
                table_id, include="stats" if include_stats else None))
        return _run(lambda: _client().list_columns(table_id))

    # --- rows ---------------------------------------------------------------

    def _list_rows(c: OrigamiClient, table_id: str, cursor: Optional[str],
                   max_pages: int, limit: Optional[int]) -> dict:
        """Follows `nextCursor` server-side up to `max_pages`; returns `cursor` (the
        next one to pass) when pages remain — the agent knows it hasn't seen everything."""
        items: list = []
        total = None
        pages = 0
        next_cursor = cursor
        while pages < max_pages:
            page = c.list_rows(table_id, cursor=next_cursor, cells="flat", limit=limit)
            pages += 1
            items.extend(_items(page))
            if isinstance(page, dict) and page.get("total") is not None:
                total = page.get("total")
            next_cursor = page.get("nextCursor") if isinstance(page, dict) else None
            if not next_cursor:
                break
        return {"table_id": table_id, "count": len(items), "total": total,
                "pages_fetched": pages, "cursor": next_cursor,
                "truncated": bool(next_cursor), "items": items}

    def _upsert_rows(c: OrigamiClient, table_id: str, rows: list, match_columns: list,
                     enrich: bool, dry_run: bool) -> dict:
        # IDENTICAL validation with and without dry_run; only the final call is skipped.
        if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
            raise _bad("op='upsert': `rows` = non-empty list of dicts {slug: value}.")
        if len(rows) > _UPSERT_MAX_ROWS:
            raise _bad(f"op='upsert': {len(rows)} rows > {_UPSERT_MAX_ROWS} per call — "
                       "split into several calls.")
        if not match_columns or not all(isinstance(m, str) and m for m in match_columns):
            raise _bad("op='upsert': `match_columns` is required (input column slugs, "
                       "e.g. ['email']) — it is the insert/update match key.")
        missing = [i for i, r in enumerate(rows)
                   if any(r.get(m) in (None, "") for m in match_columns)]
        if missing:
            raise _bad(f"op='upsert': empty match value on rows {missing[:20]} "
                       f"(columns {match_columns}) — the API refuses (MISSING_MATCH_VALUE).")
        keys = set()
        for r in rows:
            keys |= set(r)
        # Check the slugs against the REAL columns: an unknown slug or a non-input
        # column is refused here (message naming the valid slugs) instead of an
        # upstream 400 UNKNOWN_FIELDS. Explicit degraded mode if the read fails.
        inputs, all_slugs = _column_slugs(c.list_columns(table_id))
        check: dict = {"columns_available": bool(all_slugs)}
        if all_slugs:
            unknown = sorted(k for k in keys if k not in all_slugs)
            non_input = sorted(k for k in keys if k in all_slugs and k not in inputs)
            bad_match = sorted(m for m in match_columns if m not in inputs)
            if unknown or non_input or bad_match:
                raise _bad(
                    "op='upsert': keys refused — "
                    + (f"unknown slugs {unknown}; " if unknown else "")
                    + (f"non-input columns {non_input}; " if non_input else "")
                    + (f"match_columns outside the input columns {bad_match}; " if bad_match else "")
                    + f"valid input slugs: {sorted(inputs)}.")
            check.update(input_slugs_used=sorted(keys), match_columns=list(match_columns))
        if dry_run:
            return {"dry_run": True, "table_id": table_id,
                    "would_upsert": {"rows": len(rows), "match_columns": list(match_columns),
                                     "enrich": bool(enrich), "keys": sorted(keys),
                                     "sample": rows[:3]},
                    "check": check}
        receipt = c.upsert_rows(table_id, rows, match_columns, enrich=enrich)
        return {"table_id": table_id, "sent": len(rows), "receipt": receipt}

    @mcp.tool()
    def origami_rows(
        op: Literal["list", "upsert"],
        table_id: str,
        cursor: Optional[str] = None,
        max_pages: int = 1,
        limit: Optional[int] = None,
        rows: Optional[list[dict]] = None,
        match_columns: Optional[list[str]] = None,
        enrich: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Rows of an Origami table — read them (flat `{slug: value}` rows, following
        the cursor) or upsert them (the ONLY write path for rows).

        `op`:
        - **"list"**: `{count, total, pages_fetched, cursor, truncated, items}`.
          Pages are 50 rows (`limit` ≤ 200); the server follows `nextCursor` up to
          `max_pages` (cap 20). `truncated: true` + `cursor` = there is more — call
          again with that `cursor`.
        - **"upsert"** (WRITE): `rows` (1–100 dicts keyed by input-column SLUG) matched
          on `match_columns` — a row whose match values all equal an existing row
          UPDATES it, otherwise it is INSERTED. Slugs come from
          `origami_tables(op="columns")` (`items[].slug`, hyphenated); only
          `kind == "input"` columns are writable. The tool reads the columns first
          and refuses unknown / non-input slugs with the list of valid ones (the API
          would answer 400 UNKNOWN_FIELDS). `dry_run=True` runs the same validation
          and returns `{dry_run: true, would_upsert: {rows, match_columns, enrich,
          keys, sample}, check}` — nothing written. The real call returns the
          `enrichment_run` receipt `{id, counts: {inserted, updated, skipped}}`.

        Args:
            op: "list" | "upsert".
            table_id: the table.
            cursor: op="list" — `cursor` returned by a previous call (resume).
            max_pages: op="list" — pages to follow server-side (default 1, cap 20).
            limit: op="list" — rows per page (default 50, max 200).
            rows: op="upsert" — the rows, `{slug: value}` (max 100 per call).
            match_columns: op="upsert" — input-column slugs used as the match key
                (e.g. ["email"]); every row must carry a non-empty value for each.
            enrich: op="upsert" — enrich freshly INSERTED rows (spends Origami
                credits). Default False (the API default is true — here you opt in).
            dry_run: op="upsert" — validate + preview, write nothing.
        """
        if not table_id:
            raise _bad("`table_id` is required.")
        if op == "list":
            if max_pages < 1:
                raise _bad("`max_pages` ≥ 1.")
            if limit is not None and not (1 <= limit <= 200):
                raise _bad("`limit` between 1 and 200.")
            pages = min(max_pages, _ROWS_MAX_PAGES_CAP)
            return _run(lambda: _list_rows(_client(), table_id, cursor, pages, limit))
        if op == "upsert":
            return _run(lambda: _upsert_rows(_client(), table_id, rows or [],
                                             match_columns or [], enrich, dry_run))
        raise _bad(f"Invalid `op`: {op!r} (expected: list | upsert).")

    # --- upload CSV → table -------------------------------------------------

    @mcp.tool()
    def origami_upload_csv(
        workspace_id: str,
        filename: str,
        csv_text: str,
        mode: Literal["table", "append"] = "table",
        table_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Create an Origami TABLE from a CSV — the ingest verb
        `POST /workspaces/{id}/documents`, JSON with the file base64-encoded (never
        multipart). WRITE.

        `mode="table"` (default): the CSV becomes a NEW table in `workspace_id`,
        its header row = the input columns. `mode="append"` is REFUSED: Origami
        does not bind the CSV header to the table's columns and creates new ones
        on every call — rows go into an existing table with
        `origami_rows(op="upsert", table_id=…, match_columns=[…])`, which checks
        the column slugs. `dry_run=True` parses the CSV and
        returns `{dry_run: true, would_upload: {filename, mode, columns, rows,
        preview}}` (first rows) without uploading. The real call returns the per-file
        `results[]` (an entry may be `kind: "error"`); the new table then appears in
        `origami_tables(op="list", workspace_id=…)`.

        Args:
            workspace_id: target workspace (`origami_workspaces`).
            filename: name ending in `.csv` (e.g. "grossistes-fr.csv").
            csv_text: the CSV content, header row first, UTF-8.
            mode: "table" (new table, default); "append" is refused (see above).
            table_id: only named back in the refusal of mode="append".
            dry_run: preview the parsed CSV, upload nothing.
        """
        if not workspace_id:
            raise _bad("`workspace_id` is required.")
        if not filename or not filename.lower().endswith(".csv"):
            raise _bad("`filename` must end with .csv.")
        if not csv_text or not csv_text.strip():
            raise _bad("`csv_text` is empty.")
        if mode == "append":
            # Measured (#1349): Origami ignores the binding and adds as many NEW
            # columns as the header carries, answering 200 — 8 columns became 64.
            raise _bad(
                "mode='append' is refused: Origami does not bind a CSV header to the "
                "table's columns, it creates new ones on every upload (nothing was "
                "sent). Add rows to an existing table with origami_rows(op='upsert', "
                f"table_id={table_id or '<id>'!r}, rows=[…], match_columns=[…]): "
                "its keys are the column slugs of origami_tables(op='get').")
        if mode != "table":
            raise _bad(f"Invalid `mode`: {mode!r} (table).")
        parsed = _parse_csv_preview(csv_text)
        if parsed["rows"] == 0:
            raise _bad("`csv_text` has only the header: no data rows.")
        spec: dict = {"filename": filename, "mode": mode,
                      "content": base64.b64encode(csv_text.encode("utf-8")).decode("ascii")}
        if dry_run:
            return {"dry_run": True, "workspace_id": workspace_id,
                    "would_upload": {"filename": filename, "mode": mode, **parsed}}
        result = _run(lambda: _client().upload_documents(workspace_id, [spec]))
        # The id of the created table is WHAT the caller wants: it conditions the next
        # call (upsert, campaign). Surfaced at the top level rather than left at
        # `result.results[0].table.id` — measured: a harness missed it at that depth.
        # A `kind: "error"` entry is surfaced too, instead of a silent success.
        first = ((result or {}).get("results") or [{}])[0] if isinstance(result, dict) else {}
        first = first if isinstance(first, dict) else {}
        created = first.get("table") if isinstance(first.get("table"), dict) else {}
        # Shape measured on 17/08/2026: `results[0].table.{id,slug}`; a flat `tableId`
        # is accepted too.
        new_id = created.get("id") or first.get("tableId")
        out = {"workspace_id": workspace_id, "filename": filename, "mode": mode,
               "rows_sent": parsed["rows"], "columns": parsed["columns"],
               "table_id": new_id,
               "table_slug": created.get("slug"), "result": result}
        if isinstance(first, dict) and first.get("kind") == "error":
            out["error"] = first.get("error") or first.get("message") or "upload refused (kind=error)"
        return out

    # --- campaigns (read) ---------------------------------------------------

    @mcp.tool()
    def origami_campaigns(
        op: Literal["list_for_table", "get", "stats", "people"],
        table_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        cursor: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> dict:
        """Read Origami campaigns — an email + LinkedIn campaign is a set of
        sequences (one per enrolled person) that send from a table.

        `op`:
        - **"list_for_table"**: the campaigns sending from `table_id` —
          `{items: [{id, slug, name, status, peopleCount}]}`. There is NO global
          campaign list: list per table, or see every sequence of a workspace with
          `origami_sequences(workspace_id=…)`.
        - **"get"**: one campaign — `{id, name, status: draft|active|paused,
          workspaceId, tableId, channels: {email, linkedin}, settings:
          {blockPriorContacts, blockActiveDuplicates, autoTopUpEnabled}, brief}`.
        - **"stats"**: `{found, contacted, connectSent, connectAccepted,
          connectionRate, replied, replyRate, hasEmail, hasLinkedin}`.
        - **"people"**: the enrolled people — `{items: [{sequenceId, rowId, recipient,
          sendStatus, stopReason, fitScore, fitExplanation, profile, addedAt}],
          total, nextCursor}`; `status` = CSV of send-status buckets, `search` =
          substring; pass `nextCursor` back as `cursor`.

        Args:
            op: "list_for_table" | "get" | "stats" | "people".
            table_id: op="list_for_table".
            campaign_id: op="get" / "stats" / "people".
            cursor: op="people" — pagination.
            status: op="people" — CSV of send statuses to keep.
            search: op="people" — substring over recipient / identity.
        """
        if op == "list_for_table":
            if not table_id:
                raise _bad("op='list_for_table': `table_id` is required.")
            return _run(lambda: _client().list_campaigns(table_id))
        if op not in ("get", "stats", "people"):
            raise _bad(f"Invalid `op`: {op!r} (expected: list_for_table | get | stats | people).")
        if not campaign_id:
            raise _bad(f"op='{op}': `campaign_id` is required.")
        if op == "get":
            return _run(lambda: _client().get_campaign(campaign_id))
        if op == "stats":
            return _run(lambda: _client().campaign_stats(campaign_id))
        return _run(lambda: _client().campaign_people(
            campaign_id, cursor=cursor, status=status, search=search))

    # --- campaigns (write) --------------------------------------------------

    @mcp.tool()
    def origami_campaign_create(
        table_id: str,
        instructions: str,
        block_prior_contacts: bool = True,
        block_active_duplicates: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Ask Origami's agent to DRAFT a campaign on a table (WRITE — creates the
        campaign and its per-person sequences; sends NOTHING — launching is a
        separate, explicit tool).

        The call is agentic: `instructions` (1–10 000 chars: audience, channels,
        tone, offer, follow-ups…) is handed to the Origami agent, which answers
        202 `{agent: {id}, run: {id}, table}`. Poll `origami_run_get(agent_id,
        run_id)` until `status != "running"`; the drafted campaign then appears in
        `origami_campaigns(op="list_for_table", table_id=…)`. Review it (`get`,
        `people`) BEFORE `origami_campaign_launch`.

        ⚠️ **These two settings govern a campaign this call CREATES — not one the
        agent enrols into.** If your brief makes the Origami agent add people to a
        campaign that already exists, that campaign's own settings apply and yours
        have NO effect, even though they are echoed back to you. Nothing in this
        connector updates an existing campaign's settings: read what actually
        governs with `origami_campaigns(op="get")` → `settings`, and if it is wrong
        the change is a human action in Origami.

        ⚠️ **Check the result, do not trust the run's prose.** The agent answers in
        words; those words are not a measurement. A run can end `completed` having
        done NOTHING, with prose that names a campaign and a slug that were never
        created (measured 2026-09-09: three times in a row on one table) — or
        report people added while its action list is empty. `origami_run_get`
        REFUSES such a run (error `aucune_action`, nothing reachable was created:
        verify the table with `origami_campaigns(op="list_for_table")`, then
        relaunch ONCE — such a run may still leave a draft shell visible only in the
        Origami interface, invisible to every verb here, and each relaunch adds
        one; tell the human to check the campaign list in Origami). The
        state that settles it is the campaign itself: if found rises while
        contacted does not, the new sequences have no recipient and nothing will
        ever send to them.

        Settings — REQUESTED, not guaranteed: Origami has been measured ignoring
        them on a campaign this call created (2026-09-13, reproduced). Always read
        what governs with `origami_campaigns(op="get")` → `settings` once the run
        is done; stating the setting in words in `instructions` as well has made it
        stick in practice. If it is still wrong, the fix is a human toggle in
        Origami:
        - `block_prior_contacts=True` (default): auto-cancels every person who was
          EVER enrolled in a previous campaign — INCLUDING people who sat in a
          deleted, never-sent draft. Pass False ONLY when the prior enrolments were
          drafts that never actually sent to anyone; keep True to protect real
          past recipients from a second cold approach.
        - `block_active_duplicates=True` (default): auto-cancels people who are
          currently active in another campaign.

        `dry_run=True` validates and returns `{dry_run: true, would_create: {...}}`
        without calling Origami. Errors: 402 INSUFFICIENT_CREDITS, 409 AGENT_BUSY
        (an agent run is already going on that table — poll it), 429
        CONCURRENT_LIMIT_EXCEEDED (plan cap on concurrent agent runs).

        Args:
            table_id: the table the campaign sends from.
            instructions: the brief for the Origami agent.
            block_prior_contacts: suppress anyone previously enrolled (even in
                deleted unsent drafts). Default True.
            block_active_duplicates: suppress people active in another campaign.
                Default True.
            dry_run: preview only.
        """
        if not table_id:
            raise _bad("`table_id` is required.")
        if not instructions or not instructions.strip():
            raise _bad("`instructions` is required (the campaign brief).")
        if len(instructions) > 10_000:
            raise _bad("`instructions` ≤ 10 000 characters.")
        settings = {"blockPriorContacts": bool(block_prior_contacts),
                    "blockActiveDuplicates": bool(block_active_duplicates)}
        if dry_run:
            return {"dry_run": True, "table_id": table_id,
                    "would_create": {"instructions": instructions, "settings": settings},
                    "next": "origami_run_get(agent_id, run_id) then origami_campaigns(op='list_for_table')"}
        result = _run(lambda: _client().create_campaign(table_id, instructions, settings=settings))
        agent_id = ((result or {}).get("agent") or {}).get("id")
        run_id = ((result or {}).get("run") or {}).get("id")
        return {"table_id": table_id, "agent_id": agent_id, "run_id": run_id,
                # #627: `settings` is what was REQUESTED, not what applies.
                # The Origami agent may enrol into a campaign that ALREADY exists;
                # THAT campaign's settings then govern, and ours have
                # no effect — while still being returned here, which makes them read
                # as granted. The echo stays (callers read it), the note says
                # what it is worth.
                "settings": settings,
                "settings_note": (
                    "settings REQUESTED for a campaign created by this call. If "
                    "the agent enrols into an EXISTING campaign, that campaign's "
                    "settings apply and these have no "
                    "effect — read what really governs with "
                    "origami_campaigns(op='get', campaign_id=…) → settings. No "
                    "verb changes the settings of an existing campaign."),
                "response": result,
                "next": ("poll origami_run_get(agent_id, run_id) until status != 'running', "
                         "then origami_campaigns(op='list_for_table', table_id) — nothing "
                         "is sent until origami_campaign_launch(dry_run=False)")}

    @mcp.tool()
    def origami_run_get(agent_id: str, run_id: str, include: Optional[str] = None) -> dict:
        """Poll an Origami agent run — the follow-up of `origami_campaign_create`
        (which returns `agent_id` + `run_id`).

        `GET /agents/{agent_id}/runs/{run_id}` — there is NO `GET /runs/{id}`, the
        run lives under its agent. Returns the run object: `status` ("running" until
        terminal), `steps`, `response` (tables touched, transcript with
        `include="transcript"`, economics with `include="stats"`). Poll until
        `status != "running"`, then read the drafted campaign with
        `origami_campaigns`. A terminal run whose action list is EMPTY is
        REFUSED (error `aucune_action`): it created nothing reachable, whatever its
        prose says — verify with `origami_campaigns(op="list_for_table")`, then
        relaunch `origami_campaign_create` ONCE (a refused run may leave a draft
        shell visible only in the Origami interface; each relaunch adds one).

        Args:
            agent_id: from `origami_campaign_create` → `agent_id`.
            run_id: from `origami_campaign_create` → `run_id`.
            include: optional CSV of "stats", "transcript".
        """
        if not agent_id or not run_id:
            raise _bad("`agent_id` and `run_id` are required (returned by origami_campaign_create).")
        res = _run(lambda: _client().get_run(agent_id, run_id, include=include))
        return _refuse_si_rien_n_a_ete_fait(res)

    def _launch(c: OrigamiClient, campaign_id: str, dry_run: bool) -> dict:
        campaign = c.get_campaign(campaign_id)
        summary = {k: campaign.get(k) for k in ("id", "name", "status", "channels",
                                                 "settings", "tableId", "outOfLeads")}
        if dry_run:
            preview = c.launch_campaign(campaign_id, dry_run=True)
            return {"dry_run": True, "campaign": summary, "preview": preview,
                    "note": ("nothing was sent — pass dry_run=False to launch; that call "
                             "sends emails / LinkedIn messages to real people")}
        result = c.launch_campaign(campaign_id, dry_run=False)
        blocked = (result.get("launch") or {}).get("blocked") if isinstance(result, dict) else None
        out = {"campaign": summary, "result": result,
               "launched": result.get("launched") if isinstance(result, dict) else None}
        if blocked:
            out["blocked_missing_channels"] = blocked.get("missingChannels")
            out["note"] = ("LAUNCH BLOCKED — no connected sending account for these "
                           f"channels: {blocked.get('missingChannels')}. {blocked.get('message')} "
                           "Nothing was sent; connect the account in Origami, then relaunch.")
        return out

    @mcp.tool()
    def origami_campaign_launch(campaign_id: str, dry_run: bool = True) -> dict:
        """LAUNCH an Origami campaign — this SENDS emails and LinkedIn messages to
        the enrolled people. IRREVERSIBLE once messages leave.

        `dry_run` is **True by default**: the tool then reads the campaign (status,
        channels, settings) and asks Origami's own `?dryRun=true` preview
        (`{dryRun: true, campaignId, wouldLaunch}`) — nothing is sent. To actually
        launch you MUST pass `dry_run=False`, after reviewing the people
        (`origami_campaigns(op="people")`) and the drafted copy.

        The real launch marks the campaign `active` and runs the launch pipeline
        (sender gate, duplicate auto-cancel, per-account scheduling); it is
        idempotent on an already-active campaign. Read the result:
        `launched` = drafts scheduled; `result.launch.{scheduled, firstScheduledAt,
        missingRecipientCount, duplicateActiveCancelledCount,
        duplicatePriorCancelledCount}`. If `result.launch.blocked` is present
        (`blocked.missingChannels`, echoed as `blocked_missing_channels`), NO
        sending account is connected for those channels and NOTHING was sent —
        connect the email / LinkedIn account in Origami, then relaunch. There are
        no override knobs: send windows, daily caps and spacing are the campaign's
        own settings, set through the agent.

        Args:
            campaign_id: the campaign (`origami_campaigns(op="list_for_table")`).
            dry_run: default True = preview only. Pass False to send.
        """
        if not campaign_id:
            raise _bad("`campaign_id` is required.")
        return _run(lambda: _launch(_client(), campaign_id, dry_run))

    @mcp.tool()
    def origami_campaign_pause(campaign_id: str, dry_run: bool = False) -> dict:
        """Pause an active Origami campaign — halts further sends (idempotent:
        pausing a paused campaign is a no-op). WRITE.

        `dry_run=True` asks Origami's `?dryRun=true` → `{dryRun: true, campaignId,
        wouldPause}`, no writes. The real call returns the transition with
        `pause: {stoppedSequences, haltedSteps, inFlightSending, alreadyPaused}` —
        `inFlightSending` messages already handed to the provider still go out.
        """
        if not campaign_id:
            raise _bad("`campaign_id` is required.")
        result = _run(lambda: _client().pause_campaign(campaign_id, dry_run=dry_run))
        return {"dry_run": True, "preview": result} if dry_run else result

    @mcp.tool()
    def origami_campaign_resume(campaign_id: str, dry_run: bool = False) -> dict:
        """Resume a paused Origami campaign from where its sequences stopped
        (idempotent) — this SENDS again. WRITE.

        `dry_run=True` asks Origami's `?dryRun=true` → `{dryRun: true, campaignId,
        wouldResume}`, no writes. The real call returns the transition with
        `resume: {resumedSequences, noAccountSequences, missingChannels}` —
        `missingChannels` non-empty means those channels have no connected sending
        account and their sequences did not resume.
        """
        if not campaign_id:
            raise _bad("`campaign_id` is required.")
        result = _run(lambda: _client().resume_campaign(campaign_id, dry_run=dry_run))
        return {"dry_run": True, "preview": result} if dry_run else result

    def _delete(c: OrigamiClient, campaign_id: str, confirm: bool, dry_run: bool) -> dict:
        if not confirm or dry_run:
            # Step 1 (or forced preview): DELETE without confirm = impact preview, nothing
            # is removed. `dryRun=true` forces the preview even if confirm is set.
            preview = c.delete_campaign(campaign_id, confirm=confirm, dry_run=dry_run)
            return {"dry_run": True, "campaign_id": campaign_id, "deleted": False,
                    "preview": preview,
                    "note": ("nothing removed — pass confirm=True (and dry_run=False) to "
                             "delete; the tool then re-reads the campaign and reports "
                             "whether it is really gone")}
        # Step 2: real deletion, then re-GET — the 2nd step may answer 200
        # without deleting; only a 404 proves it is gone.
        result = c.delete_campaign(campaign_id, confirm=True)
        really_gone: Optional[bool]
        after: Any = None
        try:
            after = c.get_campaign(campaign_id)
            really_gone = False
        except UpstreamHTTPError as e:
            if e.status_code == 404:
                really_gone = True
            else:
                raise
        out = {"campaign_id": campaign_id, "result": result, "really_deleted": really_gone}
        if not really_gone:
            out["after"] = {k: (after or {}).get(k) for k in ("id", "name", "status")}
            out["note"] = ("Origami answered the delete but the campaign is STILL readable "
                           "(no 404 on re-read) — treat it as NOT deleted; check its status "
                           "and retry or delete it in the Origami UI.")
        return out

    @mcp.tool()
    def origami_campaign_delete(
        campaign_id: str,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Delete an Origami campaign — TWO-STEP, and verified. WRITE.

        Step 1 (`confirm=False`, default): Origami returns the impact preview
        `{id, name, confirmationRequired: true, status}`; nothing is removed.
        Step 2 (`confirm=True`, `dry_run=False`): soft-deletes the campaign
        (its picker halts instantly) and cancels its orphaned sequences. Because
        the second step can answer 200 WITHOUT deleting, the tool re-reads the
        campaign afterwards: `really_deleted: true` only if the re-read is a 404;
        otherwise `really_deleted: false` + the current status — do not report it
        as deleted. `dry_run=True` forces the preview even with `confirm=True`.

        Args:
            campaign_id: the campaign.
            confirm: True to actually delete (step 2).
            dry_run: preview only, even if confirm=True.
        """
        if not campaign_id:
            raise _bad("`campaign_id` is required.")
        return _run(lambda: _delete(_client(), campaign_id, confirm, dry_run))

    # --- sequences ----------------------------------------------------------

    def _list_sequences(c: OrigamiClient, workspace_id: str, cursor: Optional[str],
                        max_pages: int, status: Optional[str], channel: Optional[str],
                        recipient: Optional[str]) -> dict:
        """Follows `nextCursor` server-side up to `max_pages` (50 sequences/page), like
        `_list_rows`. Returns `cursor` when pages remain, `truncated: true` — the agent
        knows it hasn't seen everything. Adds `campaign_ids`, the DISTINCT list of
        campaigns encountered: it is the only way to enumerate a workspace's
        campaigns, and a first page alone suggests one where there are four
        (measured on 17/08/2026: 50 sequences / 1 campaign on one page, 369 / 4 in all)."""
        items: list = []
        pages = 0
        next_cursor = cursor
        while pages < max_pages:
            page = c.list_sequences(workspace_id, cursor=next_cursor, status=status,
                                    channel=channel, recipient=recipient)
            pages += 1
            items.extend(_items(page))
            next_cursor = page.get("nextCursor") if isinstance(page, dict) else None
            if not next_cursor:
                break
        campaign_ids = sorted({s.get("campaignId") for s in items
                               if isinstance(s, dict) and s.get("campaignId")})
        return {"workspace_id": workspace_id, "count": len(items),
                "campaign_ids": campaign_ids, "pages_fetched": pages,
                "cursor": next_cursor, "truncated": bool(next_cursor), "items": items}

    @mcp.tool()
    def origami_sequences(
        workspace_id: Optional[str] = None,
        sequence_id: Optional[str] = None,
        cursor: Optional[str] = None,
        max_pages: int = 10,
        status: Optional[str] = None,
        channel: Optional[str] = None,
        recipient: Optional[str] = None,
    ) -> dict:
        """Origami sequences — one sequence = one enrolled person in a campaign.

        Pass exactly one of:
        - `workspace_id` → `GET /sequences?workspaceId=`: EVERY sequence of the
          workspace, each with its `campaignId`, `status`, `sendStatus`,
          `stopReason`, `tableId`, `rowId` — the only view that sees all campaigns
          of a workspace at once (there is no global campaign list). Pages are 50;
          the tool follows `nextCursor` server-side up to `max_pages` (default 10 =
          500 sequences) and returns `campaign_ids`, the DISTINCT campaigns seen,
          plus `truncated`/`cursor` when more remain — pass `cursor` back to
          continue. Filters `status` / `channel` (email|linkedin) / `recipient`.
        - `sequence_id` → the sequence with its steps inline (message copy per
          step; provider internals redacted).

        Args:
            workspace_id: list mode — the workspace.
            sequence_id: get mode — one sequence.
            cursor: list mode — resume from a previous `cursor`.
            max_pages: list mode — pages of 50 to fetch server-side (default 10).
            status: list mode — filter on sequence status.
            channel: list mode — "email" | "linkedin".
            recipient: list mode — filter on recipient.
        """
        if bool(workspace_id) == bool(sequence_id):
            raise _bad("Pass exactly one of `workspace_id` (list) or `sequence_id` (detail).")
        if sequence_id:
            return _run(lambda: _client().get_sequence(sequence_id))
        if max_pages < 1:
            raise _bad("`max_pages` must be ≥ 1.")
        return _run(lambda: _list_sequences(_client(), workspace_id, cursor, max_pages,
                                            status, channel, recipient))
