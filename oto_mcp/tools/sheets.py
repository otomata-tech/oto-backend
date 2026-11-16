"""Google Sheets — oto-core surface (SheetsClient) exposed per-user, multi-account.

Editing of spreadsheets belonging to the user (different from the datastore, which is
a native PG spine — ADR 0016). `spreadsheets` scope. Default account or targeted
by `account` (email). Strictly per-user access via OAuth.

**Consolidated surface (ADR 0047 §Amendment, applied to the `sheets` product of the
`google` connector)**: one tool per business OBJECT, the verb in the `op` parameter — `sheets_spreadsheet`
(metadata/read/write/clear), all scoped by the SAME `spreadsheet_id`, and `range` on
three of them. The namespace does not change (`sheets_*`), nor does the credential (a
single Google OAuth for all its products).

`sheets_create` stays ALONE: it is the only op that does not take a `spreadsheet_id`
(it PRODUCES one) and the only one that takes `title` — its parameters overlap none
of its neighbours'. A disjoint variant weighs on the schema what the separate tool
used to; and merging it would make `spreadsheet_id` optional on ops that require it,
that is, would move into the body a guard that the signature holds today.

⚠️ **This module WRITES to the user's data**: `op="write"` overwrites the targeted
range and `op="clear"` erases its values. Two consequences wired in here, not only
documented: the default `op` is a READ (`metadata`), and `range` has a default value
ONLY for `op="read"` — a write or a clear without an explicit range is
refused, never widened to the whole sheet.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS

from .. import access
from ..auth import google as google_oauth

# Ops of `sheets_spreadsheet`. Checked BEFORE any client is built: an unknown op
# must reach no client method, never fall back on a default.
_SPREADSHEET_OPS = ("metadata", "read", "write", "clear", "add_sheet", "rename_sheet",
                    "delete_sheet", "delete_rows", "number_format", "copy_sheet", "format",
                    "column_width", "freeze", "dropdown")
_SPREADSHEET_OPS_HINT = ("op must be 'metadata', 'read', 'write', 'clear', 'add_sheet', "
                        "'rename_sheet', 'delete_sheet', 'delete_rows', 'number_format', "
                        "'copy_sheet', 'format', 'column_width', 'freeze' or 'dropdown'")

# The two ops that DESTROY data (a tab and everything in it ; rows, the ones below
# moving up) are dry-run by default (signals #1384/#1386, 10/10/2026): the call says
# what would go, `dry_run=false` does it. `clear` keeps its historical contract
# (explicit range, no dry-run): it empties values, the cells stay.
_DRY_RUN_OPS = ("delete_sheet", "delete_rows")

# Default range of the READ only (historical contract of `sheets_read`). Neither
# write nor clear inherit it: "the whole sheet" is an acceptable default for
# reading, never for overwriting or emptying.
_READ_DEFAULT_RANGE = "A:ZZ"

# A CSV written from `source`: bounded well below file_source's default, a sheet
# being read and written cell by cell.
_CSV_MAX_BYTES = 5 * 1024 * 1024


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="sheets")
    except RuntimeError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    from oto.tools.google.sheets.lib.sheets_client import SheetsClient
    return SheetsClient(credentials=creds)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 batch 2 — see gmail.py::_client_for_user_async for the
# rationale (same token-refresh mechanism, same method).
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google did not respond within {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(token refresh) — retry.")


def _values_from_source(source: dict, allow_formulas: bool) -> list[list[Any]]:
    """The rows of a CSV resolved server-side (`file_source`), typed by oto-core:
    the model never copies the values. Off the event loop (HTTP read)."""
    from .. import file_source
    from oto.tools.google.sheets.lib.sheets_client import values_from_csv
    try:
        rf = file_source.resolve(source, max_bytes=_CSV_MAX_BYTES)
    except file_source.FileSourceError as e:
        raise _bad(str(e)) from None
    try:
        text = rf.data.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad("source: the CSV is not UTF-8 — have it served in UTF-8.") from None
    rows = values_from_csv(text, formulas=allow_formulas)
    if not rows:
        raise _bad("source: the CSV has no rows — nothing written.")
    return rows


def _need(value, name: str, op: str):
    """Required argument for THIS op — actionable error, never a fallback.

    On a write op, a fallback would be damage: `op="clear"` without a range
    would fall back on the read's default range, and so empty the entire sheet.
    """
    if value is None:
        raise _bad(f"op='{op}' requires {name}")
    return value


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def sheets_create(title: str, sheets: Optional[list[str]] = None,
                            account: Optional[str] = None) -> dict:
        """Create a new empty Google spreadsheet. Returns {id, title, url, sheets}.

        Name the tabs with `sheets` (in order): without it, Google creates ONE tab
        named in the account's locale (`Sheet1`, `Feuille 1`…), and a write to
        `'Prospects'!A1` right after fails because no such tab exists.

        Args:
            title: the new spreadsheet's title.
            sheets: the tab titles to create, in order (e.g. ["Hypothèses", "P&L"]).
                Omitted = Google's single default tab.
            account: Google account (email) to act as — same choice as `_account`.
        """
        client = await _client_for_user_async(account)
        return await asyncio.to_thread(client.create, title, sheets)

    @mcp.tool()
    async def sheets_spreadsheet(
        spreadsheet_id: str,
        op: Literal["metadata", "read", "write", "clear", "add_sheet", "rename_sheet",
                    "delete_sheet", "delete_rows", "number_format", "copy_sheet", "format",
                    "column_width", "freeze", "dropdown"] = "metadata",
        range: Optional[str] = None,
        values: Optional[list[list[Any]]] = None,
        formatted: bool = True,
        append: bool = False,
        source: Optional[dict] = None,
        allow_formulas: bool = False,
        sheet: Optional[str] = None,
        title: Optional[str] = None,
        index: Optional[int] = None,
        start_row: Optional[int] = None,
        end_row: Optional[int] = None,
        pattern: Optional[str] = None,
        type: str = "NUMBER",
        to_spreadsheet_id: Optional[str] = None,
        style: Optional[dict] = None,
        width: Optional[int] = None,
        frozen_rows: Optional[int] = None,
        frozen_cols: Optional[int] = None,
        options: Optional[list[str]] = None,
        dry_run: bool = True,
        account: Optional[str] = None,
    ) -> dict:
        """An existing spreadsheet: its cells (describe, read, write, clear), its tabs
        and rows, and their formatting.

        `op`:
        - **"metadata"** (default): get a spreadsheet's metadata: title + the
          sheets/tabs it contains (id, title, rows, cols).
        - **"read"**: read values from a range (A1 notation, e.g. 'Sheet1!A1:D20'
          or 'A:ZZ'). `range` omitted = 'A:ZZ'. Returns {rows: [[...], ...], count}.
        - **"write"**: write a 2-D array of values to a range (A1 notation required).
          `append`: False (default) OVERWRITES the range ; True appends rows after
          the existing data (no overwrite), starting at the range's FIRST column.
          Instead of `values`, `source` writes a CSV oto fetches itself (e.g.
          `{"kind": "api", "path": "/export.csv"}` through your `api` connector):
          the values never go through you. A strict number goes as a number, a cell
          starting with `'` stays text as is (long ids), a cell that would be a
          formula is written as text unless `allow_formulas=True`.
        - **"clear"**: clear all values in a range (keeps formatting). Destructive —
          `range` is required, it has no default here.
        - **"add_sheet"** (`title`, `index`) ; **"rename_sheet"** (`sheet` → `title`).
        - **"delete_sheet"** (the tab and EVERYTHING in it) and **"delete_rows"**
          (`start_row`..`end_row`, 1-based inclusive): irreversible, so **dry-run by
          default** — the call says what would go ; `dry_run=false` does it.
        - **"number_format"**: `pattern` (`#,##0.00`, `0.0%`, `dd/mm/yyyy`) and
          `type` applied to every cell of `range` (e.g. `'P&L'!B2:D`).
        - **"copy_sheet"**: copy the tab `sheet` of this spreadsheet — values AND
          formatting (widths, frozen rows, colours, drop-downs, conditional formats) —
          into `to_spreadsheet_id` (this spreadsheet if omitted), named `title`, at
          `index`. The source is only read: copy a user's template tab into the file
          you produce, then write your data into the copy, to get their exact layout.
        - **"format"**: `style` applied to every cell of `range`; only the keys given
          change — `background`/`color` (`#rrggbb`), `bold`, `italic`, `font_size`,
          `wrap` (true = wrap text), `align` (left|center|right), `valign`
          (top|middle|bottom). E.g. a grey wrapped header: `range="'Suivi'!A1:Z1"`,
          `style={"background": "#d9d9d9", "bold": true, "wrap": true}`.
        - **"column_width"**: `width` in pixels for the columns of `range` (`'Suivi'!B:D`).
        - **"freeze"**: freeze the first `frozen_rows` rows and/or `frozen_cols`
          columns of `sheet` (0 unfreezes).
        - **"dropdown"**: a drop-down list of `options` on every cell of `range`, other
          values rejected ; `options=[]` removes it.
        An unknown tab is refused, naming the existing ones.

        Args:
            spreadsheet_id: the spreadsheet to act on (every op).
            op: the verb, listed above ; metadata (default).
            range: A1 notation, e.g. 'Sheet1!A1:D20' or 'A:ZZ'. op="read" — optional
                ('A:ZZ' if omitted) ; op="write"/"clear"/"number_format"/"format"/
                "column_width"/"dropdown" — REQUIRED.
            values: op="write" — the 2-D array of values to write. Exclusive with
                `source`.
            source: op="write" — a CSV fetched server-side instead of `values`:
                `{"kind": "api", "path": "/…", "params": {…}}` (GET through your
                `api` connector, auth injected), or any file reference
                (drive, url, project_file). UTF-8, comma, RFC 4180 quotes.
            allow_formulas: op="write" with `source` — False (default) writes a cell
                starting with = + - @ as TEXT (a third party's CSV cannot plant a
                formula in your sheet) ; True lets formulas through.
            formatted: op="read" — True = display strings (FORMATTED_VALUE) ;
                False = raw values.
            append: op="write" — False (default) OVERWRITES the range ; True appends
                rows after the existing data (no overwrite), from the range's first
                column.
            sheet: op="rename_sheet"/"delete_sheet"/"delete_rows"/"copy_sheet"/
                "freeze" — the tab, by title or numeric id.
            title: op="add_sheet" — the new tab's title ; op="rename_sheet" — the new
                title ; op="copy_sheet" — the copy's title (Google's `Copy of …` if
                omitted).
            index: op="add_sheet"/"copy_sheet" — 0-based position of the new tab (end
                by default).
            start_row: op="delete_rows" — first row to delete (1-based, as displayed).
            end_row: op="delete_rows" — last row to delete, inclusive.
            pattern: op="number_format" — the format pattern, as in the sheet UI.
            type: op="number_format" — NUMBER (default) | CURRENCY | PERCENT | DATE |
                TIME | DATE_TIME | SCIENTIFIC | TEXT.
            to_spreadsheet_id: op="copy_sheet" — the spreadsheet receiving the copy
                (this one if omitted).
            style: op="format" — the cell style, keys as listed above.
            width: op="column_width" — pixels.
            frozen_rows: op="freeze" — rows frozen at the top (0 = none).
            frozen_cols: op="freeze" — columns frozen at the left (0 = none).
            options: op="dropdown" — the choices of the list ; [] removes it.
            dry_run: op="delete_sheet"/"delete_rows" — True (default) only says what
                would be deleted ; False deletes.
            account: Google account (email) to act as — same choice as `_account`.
        """
        if op not in _SPREADSHEET_OPS:
            raise _bad(_SPREADSHEET_OPS_HINT)

        client = await _client_for_user_async(account)
        from oto.tools.google.sheets.lib.sheets_client import SheetsClientError

        def _structurel(fn, *args):
            """A tab/row/format op: a refusal of the client (tab unknown, bounds, type)
            comes back as an actionable error, never a bare internal error."""
            try:
                return fn(*args)
            except SheetsClientError as e:
                raise _bad(str(e)) from None

        if op == "add_sheet":
            return await asyncio.to_thread(
                _structurel, client.add_sheet, spreadsheet_id, _need(title, "title", op), index)
        if op == "rename_sheet":
            return await asyncio.to_thread(
                _structurel, client.rename_sheet, spreadsheet_id,
                _need(sheet, "sheet", op), _need(title, "title", op))
        if op in _DRY_RUN_OPS:
            _need(sheet, "sheet", op)
            if op == "delete_rows":
                _need(start_row, "start_row", op)
                _need(end_row, "end_row", op)
            if dry_run:
                sid = await asyncio.to_thread(_structurel, client.sheet_id, spreadsheet_id, sheet)
                if op == "delete_rows" and (start_row < 1 or end_row < start_row):
                    raise _bad("op='delete_rows': start_row >= 1 and end_row >= start_row")
                would = ({"tab": sheet, "id": sid, "everything_in_it": True} if op == "delete_sheet"
                         else {"tab": sheet, "id": sid, "from_row": start_row, "to_row": end_row,
                               "rows": end_row - start_row + 1})
                return {"dry_run": True, "would_delete": would,
                        "note": "Nothing deleted. Irreversible: call again with dry_run=false."}
            if op == "delete_sheet":
                return await asyncio.to_thread(_structurel, client.delete_sheet, spreadsheet_id, sheet)
            return await asyncio.to_thread(
                _structurel, client.delete_rows, spreadsheet_id, sheet, start_row, end_row)
        if op == "number_format":
            return await asyncio.to_thread(
                _structurel, client.number_format, spreadsheet_id,
                _need(range, "range", op), _need(pattern, "pattern", op), type)

        if op == "copy_sheet":
            return await asyncio.to_thread(
                _structurel, client.copy_sheet, spreadsheet_id, _need(sheet, "sheet", op),
                to_spreadsheet_id, title, index)
        if op == "format":
            return await asyncio.to_thread(
                _structurel, client.format, spreadsheet_id, _need(range, "range", op),
                _need(style, "style", op))
        if op == "column_width":
            return await asyncio.to_thread(
                _structurel, client.column_width, spreadsheet_id,
                _need(range, "range", op), _need(width, "width", op))
        if op == "freeze":
            if frozen_rows is None and frozen_cols is None:
                raise _bad("op='freeze' requires frozen_rows and/or frozen_cols")
            return await asyncio.to_thread(
                _structurel, client.freeze, spreadsheet_id, _need(sheet, "sheet", op),
                frozen_rows, frozen_cols)
        if op == "dropdown":
            return await asyncio.to_thread(
                _structurel, client.dropdown, spreadsheet_id, _need(range, "range", op),
                _need(options, "options", op))

        if op == "metadata":
            return await asyncio.to_thread(client.get_metadata, spreadsheet_id)
        if op == "read":
            render = "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE"
            rows = await asyncio.to_thread(
                client.read, spreadsheet_id, range or _READ_DEFAULT_RANGE, render)
            return {"rows": rows, "count": len(rows)}
        if op == "write":
            _need(range, "range", op)
            if (values is None) == (source is None):
                raise _bad("op='write' takes `values` OR `source` — exactly one of them")
            if allow_formulas and source is None:
                raise _bad("`allow_formulas` only applies to a `source` CSV")
            if source is not None:
                values = await asyncio.to_thread(_values_from_source, source,
                                                 allow_formulas)
            if append:
                from oto.tools.google.sheets.lib.sheets_client import SheetsClientError
                try:
                    return await asyncio.to_thread(
                        client.append, spreadsheet_id, range, values)
                except SheetsClientError as e:
                    # The row landed outside the requested columns: say where, the
                    # taxonomy would otherwise serve a bare "internal error".
                    raise McpError(ErrorData(code=INTERNAL_ERROR, message=str(e))) from e
            return await asyncio.to_thread(client.write, spreadsheet_id, range, values)
        if op == "clear":
            return await asyncio.to_thread(
                client.clear, spreadsheet_id, _need(range, "range", op))
        # Unreachable as long as `_SPREADSHEET_OPS` and the branches above say the
        # same thing — and that is precisely why the guard stays: an op added to the tuple
        # without its branch would otherwise fall into the LAST one, and so erase cells.
        raise _bad(_SPREADSHEET_OPS_HINT)
