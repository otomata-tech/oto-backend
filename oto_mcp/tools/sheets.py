"""Google Sheets — surface oto-core (SheetsClient) exposée par-utilisateur, multi-compte.

Édition de feuilles de calcul appartenant au user (différent du datastore, qui est
un spine PG natif — ADR 0016). Scope `spreadsheets`. Compte par défaut ou ciblé
par `account` (email). Accès strictement per-user via OAuth.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, google_oauth


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    from oto.tools.google.sheets.lib.sheets_client import SheetsClient
    return SheetsClient(credentials=creds)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def sheets_create(title: str, account: Optional[str] = None) -> dict:
        """Create a new empty Google spreadsheet. Returns {id, title, url}."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.create, title)

    @mcp.tool()
    async def sheets_metadata(spreadsheet_id: str, account: Optional[str] = None) -> dict:
        """Get a spreadsheet's metadata: title + the sheets/tabs it contains (id, title, rows, cols)."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.get_metadata, spreadsheet_id)

    @mcp.tool()
    async def sheets_read(
        spreadsheet_id: str,
        range: str = "A:ZZ",
        formatted: bool = True,
        account: Optional[str] = None,
    ) -> dict:
        """Read values from a range (A1 notation, e.g. 'Sheet1!A1:D20' or 'A:ZZ').

        Args:
            formatted: True = display strings (FORMATTED_VALUE) ; False = raw values.
        Returns {rows: [[...], ...], count}.
        """
        client = _client_for_user(account)
        render = "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE"
        rows = await asyncio.to_thread(client.read, spreadsheet_id, range, render)
        return {"rows": rows, "count": len(rows)}

    @mcp.tool()
    async def sheets_write(
        spreadsheet_id: str,
        range: str,
        values: list[list[Any]],
        append: bool = False,
        account: Optional[str] = None,
    ) -> dict:
        """Write a 2-D array of values to a range (A1 notation required).

        Args:
            append: False (default) OVERWRITES the range ; True appends rows
                after the existing data (no overwrite).
        """
        client = _client_for_user(account)
        if append:
            return await asyncio.to_thread(client.append, spreadsheet_id, range, values)
        return await asyncio.to_thread(client.write, spreadsheet_id, range, values)

    @mcp.tool()
    async def sheets_clear(spreadsheet_id: str, range: str, account: Optional[str] = None) -> dict:
        """Clear all values in a range (keeps formatting)."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.clear, spreadsheet_id, range)
