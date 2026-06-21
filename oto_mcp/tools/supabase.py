"""Supabase Management API — projects, auth config, logs.

Wrappe `oto.tools.supabase.client` (fonctions module-level). Le PAT (`sbp_…`)
est résolu par appel via `access.resolve_api_key("supabase")` — byo, passé en
`token=` à chaque fonction (aucun secret au niveau du process).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.supabase import client as sb

    def _token() -> str:
        key, _ = access.resolve_api_key("supabase")
        return key

    @mcp.tool()
    async def supabase_list_projects() -> dict:
        """List the Supabase projects reachable with this access token."""
        return {"projects": sb.list_projects(token=_token())}

    @mcp.tool()
    async def supabase_auth_config(project_ref: str) -> dict:
        """Auth config of a project (site_url, redirect allow-list, providers…).

        Args:
            project_ref: project ref (e.g. "doebdriroupduqpggcsj").
        """
        return sb.get_auth_config(project_ref, token=_token())

    @mcp.tool()
    async def supabase_query_logs(
        project_ref: str,
        sql: Optional[str] = None,
        source: str = "auth_logs",
        limit: int = 50,
        minutes: int = 120,
    ) -> dict:
        """Query a project's logs (Logflare via the Management API).

        Args:
            sql: Logflare SQL. If omitted, returns the latest lines of `source`.
            source: auth_logs, edge_logs, function_edge_logs, function_logs,
                postgres_logs, postgrest_logs, storage_logs…
            minutes: time window (the API requires an iso timestamp range).
        """
        return {"rows": sb.query_logs(
            project_ref, sql=sql, source=source, limit=limit,
            minutes=minutes, token=_token())}
