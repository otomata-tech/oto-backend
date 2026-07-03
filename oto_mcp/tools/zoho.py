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
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access


# Zoho héberge par data center régional ; le self-client (client_id/secret) ET le
# refresh token sont liés à leur région d'émission — un self-client `.eu` tapant
# `accounts.zoho.com` est rejeté par Zoho avec un `invalid_client` opaque. Le champ
# `data_center` du credential sélectionne les domaines API/OAuth. Régions reconnues :
_DC_DOMAINS = {
    "com": ("https://www.zohoapis.com", "https://accounts.zoho.com"),
    "eu": ("https://www.zohoapis.eu", "https://accounts.zoho.eu"),
    "in": ("https://www.zohoapis.in", "https://accounts.zoho.in"),
    "au": ("https://www.zohoapis.com.au", "https://accounts.zoho.com.au"),
    "jp": ("https://www.zohoapis.jp", "https://accounts.zoho.jp"),
    "ca": ("https://www.zohoapis.ca", "https://accounts.zohocloud.ca"),
}


def _resolve_dc_domains(data_center: Optional[str]) -> tuple[str, str]:
    """`(api_domain, accounts_url)` pour la région Zoho déclarée. Région manquante ou
    non reconnue → `McpError` actionnable, **jamais** de repli silencieux sur `com` (ce
    repli masquait la vraie cause d'un `invalid_client` : self-client posé sur une autre
    région). `com` reste pleinement valide — on ne force aucune région, on exige juste un
    choix reconnu."""
    dc = (data_center or "").strip().lower()
    if dc not in _DC_DOMAINS:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            (f"Data center Zoho non reconnu : {data_center!r}." if dc
             else "Data center Zoho manquant.")
            + " Renseigne ta région dans le champ « Data center » du connecteur Zoho —"
            " l'une de : com, eu, in, au, jp, ca. Elle est visible dans l'URL quand tu es"
            " connecté·e à Zoho (ex. crm.zoho.eu → « eu », crm.zoho.com → « com »)."
        )))
    return _DC_DOMAINS[dc]


def register(mcp: FastMCP) -> None:
    from oto.tools.zoho.client import ZohoClient

    def _client() -> ZohoClient:
        creds = access.resolve_credential_fields("zoho")
        api_domain, accounts_url = _resolve_dc_domains(creds.get("data_center"))
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
