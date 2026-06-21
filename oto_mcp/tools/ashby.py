"""Ashby ATS — candidates, jobs, applications, notes.

Wrappe `oto.tools.ashby.AshbyClient` (API key, Basic auth). Clé résolue par appel
via `access.resolve_api_key("ashby")` — byo (clé user sur /account ou credential
partagé de l'org). Pas de clé plateforme.

⚠️ Ashby est une API **RPC POST** paginée par `cursor` : passer le `nextCursor`
d'une réponse (quand `moreDataAvailable` est vrai) au paramètre `cursor` suivant.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.ashby.client import AshbyClient

    def _client() -> AshbyClient:
        key, _ = access.resolve_api_key("ashby")
        return AshbyClient(api_key=key)

    @mcp.tool()
    async def ashby_candidates(limit: int = 50, cursor: Optional[str] = None) -> dict:
        """List candidates (paginated). Returns {results, moreDataAvailable,
        nextCursor} — pass nextCursor to `cursor` for the next page."""
        return _client().list_candidates(limit=limit, cursor=cursor)

    @mcp.tool()
    async def ashby_candidate(candidate_id: str) -> dict:
        """Fetch one candidate by id."""
        return _client().get_candidate(candidate_id)

    @mcp.tool()
    async def ashby_search_candidates(
        email: Optional[str] = None, name: Optional[str] = None,
    ) -> dict:
        """Search candidates by email and/or name."""
        return _client().search_candidates(email=email, name=name)

    @mcp.tool()
    async def ashby_add_note(candidate_id: str, note: str) -> dict:
        """Add a note to a candidate."""
        return _client().add_note(candidate_id, note)

    @mcp.tool()
    async def ashby_jobs(
        limit: int = 50, cursor: Optional[str] = None, status: Optional[str] = None,
    ) -> dict:
        """List jobs. status: Open | Closed | Draft | Archived."""
        return _client().list_jobs(limit=limit, cursor=cursor, status=status)

    @mcp.tool()
    async def ashby_job(job_id: str) -> dict:
        """Fetch one job by id."""
        return _client().get_job(job_id)

    @mcp.tool()
    async def ashby_applications(
        limit: int = 50, cursor: Optional[str] = None, job_id: Optional[str] = None,
    ) -> dict:
        """List applications, optionally filtered by job_id."""
        return _client().list_applications(limit=limit, cursor=cursor, job_id=job_id)

    @mcp.tool()
    async def ashby_application(application_id: str) -> dict:
        """Fetch one application by id."""
        return _client().get_application(application_id)
