"""Zoho Desk — support tickets, threads, contacts.

Credential = OAuth2 (self-client) à 4 secrets : client_id + client_secret +
refresh_token + org_id (en-tête `orgId` requis) → modèle générique multi-champs
(ADR 0011), résolu par appel via `access.resolve_credential_fields("zohodesk")`.
byo_user. Token d'accès dérivé/caché en mémoire côté client.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.zohodesk.client import ZohoDeskClient

    def _client() -> ZohoDeskClient:
        creds = access.resolve_credential_fields("zohodesk")
        return ZohoDeskClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            refresh_token=creds.get("refresh_token"),
            org_id=creds.get("org_id"),
        )

    @mcp.tool()
    async def zohodesk_tickets(
        from_index: int = 1,
        limit: int = 50,
        department_id: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """List support tickets.

        Args:
            status: Open | On Hold | Escalated | Closed.
            sort_by: a field name (prefix with "-" for descending).
        """
        return _client().list_tickets(
            from_index=from_index, limit=limit, department_id=department_id,
            status=status, sort_by=sort_by)

    @mcp.tool()
    async def zohodesk_ticket(ticket_id: str, include: Optional[str] = None) -> dict:
        """Get one ticket. `include` = contacts,products,assignee,team…"""
        return _client().get_ticket(ticket_id, include=include)

    @mcp.tool()
    async def zohodesk_search_tickets(
        query: dict, from_index: int = 1, limit: int = 50,
    ) -> dict:
        """Search tickets. `query` = dict of field=value pairs (Zoho search params)."""
        return _client().search_tickets(query, from_index=from_index, limit=limit)

    @mcp.tool()
    async def zohodesk_create_ticket(data: dict) -> dict:
        """Create a ticket. Required: subject, departmentId, contactId (or contact)."""
        return _client().create_ticket(data)

    @mcp.tool()
    async def zohodesk_update_ticket(ticket_id: str, data: dict) -> dict:
        """Patch ticket fields (status, priority, assignee, customFields…)."""
        return _client().update_ticket(ticket_id, data)

    @mcp.tool()
    async def zohodesk_ticket_threads(ticket_id: str) -> dict:
        """List the threads (replies/comments) of a ticket."""
        return _client().list_threads(ticket_id)

    @mcp.tool()
    async def zohodesk_contacts(from_index: int = 1, limit: int = 50) -> dict:
        """List Desk contacts."""
        return _client().list_contacts(from_index=from_index, limit=limit)

    @mcp.tool()
    async def zohodesk_create_contact(data: dict) -> dict:
        """Create a Desk contact. Required: lastName. Optional: firstName, email, phone."""
        return _client().create_contact(data)

    @mcp.tool()
    async def zohodesk_departments() -> dict:
        """List Desk departments."""
        return _client().list_departments()
