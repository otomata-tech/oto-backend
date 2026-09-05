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
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /v1/users` (`list_users`, déjà dans le client), `limit=1` — le plus
    petit format disponible, Lever n'exposant ni `/me` ni solde. Basic auth
    (clé en username, mot de passe vide), lecture sans effet de bord. Aucune
    mention de coût ni de limite de débit particulière pour cet appel.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope —
    une clé Lever porte le périmètre entier du compte, pas de permission
    granulaire par ressource.
    """
    from oto.tools.lever.client import LeverClient

    LeverClient(api_key=fields["key"]).list_users(limit=1)


def register(mcp: FastMCP) -> None:
    from oto.tools.lever.client import LeverClient

    connector_verify.register("lever", _verify)

    def _client() -> LeverClient:
        key, _ = access.resolve_api_key("lever")
        return LeverClient(api_key=key)

    @mcp.tool()
    def lever_opportunities(
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
    def lever_opportunity(
        opportunity_id: str, expand: Optional[list[str]] = None,
    ) -> dict:
        """Fetch one opportunity (candidate) by id."""
        return _client().get_opportunity(opportunity_id, expand=expand)

    @mcp.tool()
    def lever_add_candidate(
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
    def lever_add_note(opportunity_id: str, value: str, perform_as: str) -> dict:
        """Add a note to an opportunity (candidate). perform_as = Lever user id."""
        return _client().add_note(opportunity_id, value, perform_as=perform_as)

    @mcp.tool()
    def lever_postings(
        limit: int = 50, offset: Optional[str] = None, state: Optional[str] = None,
    ) -> dict:
        """List postings (jobs). state: published | internal | closed | draft |
        pending | rejected."""
        return _client().list_postings(limit=limit, offset=offset, state=state)

    @mcp.tool()
    def lever_posting(posting_id: str) -> dict:
        """Fetch one posting (job) by id."""
        return _client().get_posting(posting_id)

    @mcp.tool()
    def lever_stages() -> dict:
        """List pipeline stages (reference data)."""
        return _client().list_stages()

    @mcp.tool()
    def lever_users(limit: int = 50, offset: Optional[str] = None) -> dict:
        """List Lever users (recruiters) — get an id for `perform_as`."""
        return _client().list_users(limit=limit, offset=offset)
