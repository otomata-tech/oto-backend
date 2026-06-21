"""Lever ATS — opportunities (candidats), postings, stages, notes.

Wrappe `oto.tools.lever.LeverClient` (API key, Basic auth). Clé résolue par appel
via `access.resolve_api_key("lever")` — byo (clé user sur /account ou credential
partagé de l'org). Pas de clé plateforme.

Vocabulaire : un candidat dans un pipeline = une **opportunity** ; un poste = un
**posting**. Les écritures exigent un `perform_as` (id d'un user Lever — voir
`lever_users`). Pagination : passer le `next` d'une réponse à `offset`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.lever.client import LeverClient

    def _client() -> LeverClient:
        key, _ = access.resolve_api_key("lever")
        return LeverClient(api_key=key)

    @mcp.tool()
    async def lever_opportunities(
        limit: int = 50,
        offset: Optional[str] = None,
        posting_id: Optional[str] = None,
        stage_id: Optional[str] = None,
        email: Optional[str] = None,
        expand: Optional[list[str]] = None,
    ) -> dict:
        """List opportunities (candidates). Returns {data, hasNext, next} — pass
        `next` to `offset` for the next page.

        Args:
            posting_id / stage_id: pipeline filters.
            email: filter by exact candidate email.
            expand: fields to expand (e.g. ["applications", "stage", "owner"]).
        """
        return _client().list_opportunities(
            limit=limit, offset=offset, posting_id=posting_id, stage_id=stage_id,
            email=email, expand=expand)

    @mcp.tool()
    async def lever_opportunity(
        opportunity_id: str, expand: Optional[list[str]] = None,
    ) -> dict:
        """Fetch one opportunity (candidate) by id."""
        return _client().get_opportunity(opportunity_id, expand=expand)

    @mcp.tool()
    async def lever_add_candidate(
        candidate: dict, perform_as: str, posting_ids: Optional[list[str]] = None,
    ) -> dict:
        """Create a candidate (opportunity).

        Args:
            candidate: Lever candidate object (name, emails, phones, links, tags,
                sources, …).
            perform_as: Lever user id to act as (required — see lever_users).
            posting_ids: postings to attach the candidate to.
        """
        return _client().add_candidate(
            candidate, perform_as=perform_as, posting_ids=posting_ids)

    @mcp.tool()
    async def lever_add_note(opportunity_id: str, value: str, perform_as: str) -> dict:
        """Add a note to an opportunity (candidate). perform_as = Lever user id."""
        return _client().add_note(opportunity_id, value, perform_as=perform_as)

    @mcp.tool()
    async def lever_postings(
        limit: int = 50, offset: Optional[str] = None, state: Optional[str] = None,
    ) -> dict:
        """List postings (jobs). state: published | internal | closed | draft |
        pending | rejected."""
        return _client().list_postings(limit=limit, offset=offset, state=state)

    @mcp.tool()
    async def lever_posting(posting_id: str) -> dict:
        """Fetch one posting (job) by id."""
        return _client().get_posting(posting_id)

    @mcp.tool()
    async def lever_stages() -> dict:
        """List pipeline stages (reference data)."""
        return _client().list_stages()

    @mcp.tool()
    async def lever_users(limit: int = 50, offset: Optional[str] = None) -> dict:
        """List Lever users (recruiters) — get an id for `perform_as`."""
        return _client().list_users(limit=limit, offset=offset)
