"""Recruitee ATS — candidats, offers (postes), notes.

Wrappe `oto.tools.recruitee.RecruiteeClient`. Credential à 2 champs (API token +
company id) → modèle générique multi-champs (ADR 0011), résolu par appel via
`access.resolve_credential_fields("recruitee")`. byo_user (pas de quota plateforme :
le credential EST le grant).

Vocabulaire : un poste = une **offer** ; un candidat est rattaché à une/des offers.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.recruitee.client import RecruiteeClient

    def _client() -> RecruiteeClient:
        creds = access.resolve_credential_fields("recruitee")
        return RecruiteeClient(
            api_token=creds.get("api_token"),
            company_id=creds.get("company_id"),
        )

    @mcp.tool()
    def recruitee_candidates(
        limit: int = 50, offset: int = 0,
        offer_id: Optional[int] = None, query: Optional[str] = None,
    ) -> dict:
        """List candidates (paginated).

        Args:
            offer_id: filter by job (offer).
            query: search by name/email.
        """
        return _client().list_candidates(
            limit=limit, offset=offset, offer_id=offer_id, query=query)

    @mcp.tool()
    def recruitee_candidate(candidate_id: int) -> dict:
        """Fetch one candidate by id."""
        return _client().get_candidate(candidate_id)

    @mcp.tool()
    def recruitee_create_candidate(
        candidate: dict, offer_ids: Optional[list[int]] = None,
    ) -> dict:
        """Create a candidate.

        Args:
            candidate: candidate object (name, emails, phones, social_links,
                links, cover_letter, …).
            offer_ids: jobs (offers) to attach the candidate to.
        """
        return _client().create_candidate(candidate, offer_ids=offer_ids)

    @mcp.tool()
    def recruitee_add_note(candidate_id: int, body: str) -> dict:
        """Add a note to a candidate."""
        return _client().add_note(candidate_id, body)

    @mcp.tool()
    def recruitee_offers(
        scope: Optional[str] = None, kind: Optional[str] = None,
    ) -> dict:
        """List offers (jobs). scope: active | archived | not_archived ;
        kind: job | talent_pool."""
        return _client().list_offers(scope=scope, kind=kind)

    @mcp.tool()
    def recruitee_offer(offer_id: int) -> dict:
        """Fetch one offer (job) by id."""
        return _client().get_offer(offer_id)
