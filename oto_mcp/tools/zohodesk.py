"""Zoho Desk — support tickets, threads, contacts, articles (Help Center KB).

Credential = OAuth2 (self-client) à 5 champs : client_id + client_secret +
refresh_token + org_id (en-tête `orgId` requis) + data_center (région, non-secret)
→ modèle générique multi-champs (ADR 0011), résolu par appel via
`access.resolve_credential_fields("zohodesk")`. byo_user. Token d'accès dérivé/caché
en mémoire côté client.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access

# Zoho héberge par data center régional : l'API Desk ET le refresh OAuth sont liés à
# leur région d'émission (un self-client `.eu` tapant `desk.zoho.com`/`accounts.zoho.com`
# est rejeté par un `invalid_client` opaque — même gotcha que le connecteur CRM). Le
# champ `data_center` du credential sélectionne les domaines API (`desk.zoho.<tld>`) et
# OAuth (`accounts.zoho.<tld>`). Régions reconnues :
_DC_DOMAINS = {
    "com": ("https://desk.zoho.com", "https://accounts.zoho.com"),
    "eu": ("https://desk.zoho.eu", "https://accounts.zoho.eu"),
    "in": ("https://desk.zoho.in", "https://accounts.zoho.in"),
    "au": ("https://desk.zoho.com.au", "https://accounts.zoho.com.au"),
    "jp": ("https://desk.zoho.jp", "https://accounts.zoho.jp"),
    "ca": ("https://desk.zohocloud.ca", "https://accounts.zohocloud.ca"),
}


def _resolve_dc_domains(data_center: Optional[str]) -> tuple[str, str]:
    """`(api_domain, accounts_url)` pour la région Zoho Desk déclarée. Région manquante
    ou non reconnue → `McpError` actionnable, **jamais** de repli silencieux sur `com`
    (ce repli masquait la vraie cause d'un `invalid_client` : self-client posé sur une
    autre région). `com` reste pleinement valide — on exige juste un choix reconnu."""
    dc = (data_center or "").strip().lower()
    if dc not in _DC_DOMAINS:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            (f"Data center Zoho non reconnu : {data_center!r}." if dc
             else "Data center Zoho manquant.")
            + " Renseigne ta région dans le champ « Data center » du connecteur Zoho Desk —"
            " l'une de : com, eu, in, au, jp, ca. Elle est visible dans l'URL quand tu es"
            " connecté·e à Zoho Desk (ex. desk.zoho.eu → « eu », desk.zoho.com → « com »)."
        )))
    return _DC_DOMAINS[dc]


def register(mcp: FastMCP) -> None:
    from oto.tools.zohodesk.client import ZohoDeskClient

    def _client() -> ZohoDeskClient:
        creds = access.resolve_credential_fields("zohodesk")
        api_domain, accounts_url = _resolve_dc_domains(creds.get("data_center"))
        return ZohoDeskClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            refresh_token=creds.get("refresh_token"),
            org_id=creds.get("org_id"),
            api_domain=api_domain,
            accounts_url=accounts_url,
        )

    @mcp.tool()
    def zohodesk_tickets(
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
    def zohodesk_ticket(ticket_id: str, include: Optional[str] = None) -> dict:
        """Get one ticket. `include` = contacts,products,assignee,team…"""
        return _client().get_ticket(ticket_id, include=include)

    @mcp.tool()
    def zohodesk_search_tickets(
        query: dict, from_index: int = 1, limit: int = 50,
    ) -> dict:
        """Search tickets. `query` = dict of field=value pairs (Zoho search params)."""
        return _client().search_tickets(query, from_index=from_index, limit=limit)

    @mcp.tool()
    def zohodesk_create_ticket(data: dict) -> dict:
        """Create a ticket. Required: subject, departmentId, contactId (or contact)."""
        return _client().create_ticket(data)

    @mcp.tool()
    def zohodesk_update_ticket(ticket_id: str, data: dict) -> dict:
        """Patch ticket fields (status, priority, assignee, customFields…)."""
        return _client().update_ticket(ticket_id, data)

    @mcp.tool()
    def zohodesk_ticket_threads(ticket_id: str) -> dict:
        """List the threads (replies/comments) of a ticket."""
        return _client().list_threads(ticket_id)

    @mcp.tool()
    def zohodesk_contacts(from_index: int = 1, limit: int = 50) -> dict:
        """List Desk contacts."""
        return _client().list_contacts(from_index=from_index, limit=limit)

    @mcp.tool()
    def zohodesk_create_contact(data: dict) -> dict:
        """Create a Desk contact. Required: lastName. Optional: firstName, email, phone."""
        return _client().create_contact(data)

    @mcp.tool()
    def zohodesk_departments() -> dict:
        """List Desk departments."""
        return _client().list_departments()

    @mcp.tool()
    def zohodesk_articles(
        from_index: int = 1,
        limit: int = 50,
        department_id: Optional[str] = None,
        category_id: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """List Help Center (KB) articles — metadata only (the HTML body comes
        from `zohodesk_article`).

        Args:
            status: Published | Draft | Review | Expired.
            sort_by: a field name (e.g. modifiedTime, viewCount; prefix "-" for desc).
        """
        return _client().list_articles(
            from_index=from_index, limit=limit, department_id=department_id,
            category_id=category_id, status=status, sort_by=sort_by)

    @mcp.tool()
    def zohodesk_article(article_id: str) -> dict:
        """Get one Help Center article, including its full HTML body (`answer`)."""
        return _client().get_article(article_id)

    @mcp.tool()
    def zohodesk_search_articles(
        search_str: str, from_index: int = 1, limit: int = 50,
        department_id: Optional[str] = None,
    ) -> dict:
        """Full-text search over Help Center articles."""
        return _client().search_articles(
            search_str, from_index=from_index, limit=limit,
            department_id=department_id)
