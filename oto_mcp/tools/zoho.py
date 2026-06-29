"""Zoho CRM — generic CRUD over modules (Contacts, Leads, Deals, Accounts…).

Credential = OAuth2 (self-client) à 3 secrets : client_id + client_secret +
refresh_token → modèle générique multi-champs (ADR 0011), résolu par appel via
`access.resolve_credential_fields("zoho")`. byo_user (pas de quota plateforme :
le credential EST le grant). Le token d'accès est dérivé/caché en mémoire côté
client.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.zoho.client import ZohoClient

    # Zoho héberge par data center régional ; le refresh token est lié à sa
    # région d'émission (un token .eu est rejeté par accounts.zoho.com). Le champ
    # `data_center` du credential (défaut "com" en back-compat) sélectionne les
    # domaines API/OAuth passés au client.
    _DC_DOMAINS = {
        "com": ("https://www.zohoapis.com", "https://accounts.zoho.com"),
        "eu": ("https://www.zohoapis.eu", "https://accounts.zoho.eu"),
        "in": ("https://www.zohoapis.in", "https://accounts.zoho.in"),
        "au": ("https://www.zohoapis.com.au", "https://accounts.zoho.com.au"),
        "jp": ("https://www.zohoapis.jp", "https://accounts.zoho.jp"),
        "ca": ("https://www.zohoapis.ca", "https://accounts.zohocloud.ca"),
    }

    def _client() -> ZohoClient:
        creds = access.resolve_credential_fields("zoho")
        dc = (creds.get("data_center") or "com").strip().lower()
        api_domain, accounts_url = _DC_DOMAINS.get(dc, _DC_DOMAINS["com"])
        return ZohoClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            refresh_token=creds.get("refresh_token"),
            api_domain=api_domain,
            accounts_url=accounts_url,
        )

    @mcp.tool()
    def zoho_modules() -> dict:
        """List the available CRM modules (Contacts, Leads, Deals, Accounts…)."""
        return {"modules": _client().list_modules()}

    @mcp.tool()
    def zoho_records(
        module: str,
        page: int = 1,
        per_page: int = 200,
        fields: Optional[str] = None,
    ) -> dict:
        """List records from a module.

        Args:
            module: e.g. "Contacts", "Leads", "Deals", "Accounts".
            fields: comma-separated field names. Optional — a sensible default
                set is used per known module if omitted.
        """
        return _client().list_records(module, page=page, per_page=per_page, fields=fields)

    @mcp.tool()
    def zoho_get(module: str, record_id: str) -> dict:
        """Get one record by id. {} if not found."""
        return _client().get_record(module, record_id)

    @mcp.tool()
    def zoho_search(
        module: str, criteria: str, page: int = 1, per_page: int = 200,
    ) -> dict:
        """Search records.

        Args:
            criteria: Zoho criteria, e.g. "(Email:equals:a@b.com)" or
                "(Last_Name:starts_with:Dup)".
        """
        return _client().search_records(module, criteria, page=page, per_page=per_page)

    @mcp.tool()
    def zoho_create(module: str, data: dict) -> dict:
        """Create a record in a module (data = field → value)."""
        return _client().create_record(module, data)

    @mcp.tool()
    def zoho_update(module: str, record_id: str, data: dict) -> dict:
        """Update a record's fields."""
        return _client().update_record(module, record_id, data)

    @mcp.tool()
    def zoho_delete(module: str, record_id: str) -> dict:
        """Delete a record. Irreversible."""
        return _client().delete_record(module, record_id)

    @mcp.tool()
    def zoho_notes(module: str, record_id: str) -> dict:
        """List the notes attached to a record."""
        return {"notes": _client().list_notes(module, record_id)}

    @mcp.tool()
    def zoho_create_note(
        module: str, record_id: str, title: str, content: str,
    ) -> dict:
        """Add a note to a record."""
        return _client().create_note(module, record_id, title, content)
