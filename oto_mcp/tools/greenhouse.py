"""Greenhouse Harvest API — ATS (candidats, jobs, candidatures, notes).

Wrappe `oto.tools.greenhouse.GreenhouseClient` (Harvest API key, Basic auth). Clé
résolue par appel via `access.resolve_api_key("greenhouse")` — byo (clé user sur
/account ou credential partagé de l'org). Pas de clé plateforme.

⚠️ Greenhouse exige un **`on_behalf_of`** (id d'un utilisateur Greenhouse) sur les
écritures (création de candidat, note) — récupérer un id via `greenhouse_users`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.greenhouse.client import GreenhouseClient

    def _client() -> GreenhouseClient:
        key, _ = access.resolve_api_key("greenhouse", "GREENHOUSE_API_KEY")
        return GreenhouseClient(api_key=key)

    @mcp.tool()
    async def greenhouse_candidates(
        per_page: int = 50,
        page: int = 1,
        job_id: Optional[int] = None,
        email: Optional[str] = None,
        created_after: Optional[str] = None,
        updated_after: Optional[str] = None,
    ) -> list:
        """List candidates (paginated).

        Args:
            job_id: only candidates with an application on this job.
            email: filter by exact email.
            created_after / updated_after: ISO 8601 timestamps.
        """
        return _client().list_candidates(
            per_page=per_page, page=page, job_id=job_id, email=email,
            created_after=created_after, updated_after=updated_after)

    @mcp.tool()
    async def greenhouse_candidate(candidate_id: int) -> dict:
        """Fetch one candidate by id (with their applications)."""
        return _client().get_candidate(candidate_id)

    @mcp.tool()
    async def greenhouse_add_candidate(candidate: dict, on_behalf_of: int) -> dict:
        """Create a candidate/prospect.

        Args:
            candidate: Greenhouse candidate object (first_name, last_name,
                email_addresses, phone_numbers, applications, …).
            on_behalf_of: Greenhouse user id to act as (required for writes —
                see greenhouse_users).
        """
        return _client().add_candidate(candidate, on_behalf_of=on_behalf_of)

    @mcp.tool()
    async def greenhouse_add_note(
        candidate_id: int, body: str, user_id: int, visibility: str = "public",
    ) -> dict:
        """Add a note to a candidate's activity feed.

        Args:
            user_id: Greenhouse user id authoring the note.
            visibility: admin_only | private | public.
        """
        return _client().add_note(candidate_id, body, user_id, visibility=visibility)

    @mcp.tool()
    async def greenhouse_jobs(
        per_page: int = 50, page: int = 1, status: Optional[str] = None,
    ) -> list:
        """List jobs. status: open | closed | draft."""
        return _client().list_jobs(per_page=per_page, page=page, status=status)

    @mcp.tool()
    async def greenhouse_job(job_id: int) -> dict:
        """Fetch one job by id."""
        return _client().get_job(job_id)

    @mcp.tool()
    async def greenhouse_applications(
        per_page: int = 50, page: int = 1, job_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> list:
        """List applications. status: active | rejected | hired."""
        return _client().list_applications(
            per_page=per_page, page=page, job_id=job_id, status=status)

    @mcp.tool()
    async def greenhouse_application(application_id: int) -> dict:
        """Fetch one application by id."""
        return _client().get_application(application_id)

    @mcp.tool()
    async def greenhouse_users(per_page: int = 50, page: int = 1) -> list:
        """List Greenhouse users (recruiters) — get an id for `on_behalf_of`."""
        return _client().list_users(per_page=per_page, page=page)
