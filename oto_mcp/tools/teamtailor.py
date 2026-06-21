"""Teamtailor ATS — candidats, jobs, candidatures (JSON:API).

Wrappe `oto.tools.teamtailor.TeamtailorClient` (API key dans l'en-tête
`Authorization: Token token=…`). Clé résolue par appel via
`access.resolve_api_key("teamtailor")` — byo (clé user sur /account ou credential
partagé de l'org). Pas de clé plateforme.

⚠️ Réponses au format **JSON:API** : ressources sous `data` avec `{type, id,
attributes, relationships}`. Pagination par `page_number`/`page_size`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.teamtailor.client import TeamtailorClient

    def _client() -> TeamtailorClient:
        key, _ = access.resolve_api_key("teamtailor")
        return TeamtailorClient(api_key=key)

    @mcp.tool()
    async def teamtailor_candidates(
        page_size: int = 30, page_number: int = 1, email: Optional[str] = None,
    ) -> dict:
        """List candidates (paginated, JSON:API). `email` filters by exact email."""
        return _client().list_candidates(
            page_size=page_size, page_number=page_number, email=email)

    @mcp.tool()
    async def teamtailor_candidate(candidate_id: str) -> dict:
        """Fetch one candidate by id."""
        return _client().get_candidate(candidate_id)

    @mcp.tool()
    async def teamtailor_create_candidate(attributes: dict) -> dict:
        """Create a candidate.

        Args:
            attributes: JSON:API attributes (first-name, last-name, email, phone,
                pitch, tags, …). Wrapped in {data:{type, attributes}} by the client.
        """
        return _client().create_candidate(attributes)

    @mcp.tool()
    async def teamtailor_jobs(
        page_size: int = 30, page_number: int = 1, status: Optional[str] = None,
    ) -> dict:
        """List jobs. status: open | draft | archived | unlisted."""
        return _client().list_jobs(
            page_size=page_size, page_number=page_number, status=status)

    @mcp.tool()
    async def teamtailor_job(job_id: str) -> dict:
        """Fetch one job by id."""
        return _client().get_job(job_id)

    @mcp.tool()
    async def teamtailor_job_applications(
        page_size: int = 30, page_number: int = 1, job_id: Optional[str] = None,
    ) -> dict:
        """List job applications, optionally filtered by job_id."""
        return _client().list_job_applications(
            page_size=page_size, page_number=page_number, job_id=job_id)
