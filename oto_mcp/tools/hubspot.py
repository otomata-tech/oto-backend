"""HubSpot CRM — contacts, companies, deals, tickets, notes (read + write).

Wrappe `oto.tools.hubspot.HubSpotClient` (private app token). Clé résolue par
appel via `access.resolve_api_key("hubspot")` — byo (user key sur /account ou
credential partagé de l'org). Pas de clé plateforme.

Surface générique : `object_type` = contacts | companies | deals | tickets
(ou tout objet custom) pour search/get/create/update/delete — fusion sans perte.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.hubspot.client import HubSpotClient

    def _client() -> HubSpotClient:
        key, _ = access.resolve_api_key("hubspot")
        return HubSpotClient(api_key=key)

    @mcp.tool()
    def hubspot_search(
        object_type: str,
        query: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        properties: Optional[list[str]] = None,
        limit: int = 100,
        after: Optional[str] = None,
    ) -> dict:
        """Search CRM objects.

        Args:
            object_type: contacts | companies | deals | tickets (or custom).
            query: full-text search.
            filters: list of {propertyName, operator, value} combined with AND.
                operators: EQ, NEQ, GT, GTE, LT, LTE, CONTAINS_TOKEN, HAS_PROPERTY,
                IN (then pass "values": [...] instead of "value").
            properties: property names to return.
            after: pagination cursor from a previous response (paging.next.after).
        """
        return _client().search_objects(
            object_type, query=query, filters=filters, properties=properties,
            limit=limit, after=after)

    @mcp.tool()
    def hubspot_get(
        object_type: str,
        object_id: str,
        properties: Optional[list[str]] = None,
        associations: Optional[list[str]] = None,
    ) -> dict:
        """Fetch one CRM object by id.

        Args:
            associations: other object types to return associated ids for
                (e.g. ["companies", "deals"] on a contact).
        """
        return _client().get_object(
            object_type, object_id, properties=properties, associations=associations)

    @mcp.tool()
    def hubspot_list(
        object_type: str,
        properties: Optional[list[str]] = None,
        limit: int = 100,
        after: Optional[str] = None,
    ) -> dict:
        """List CRM objects of a type (paginated via `after`)."""
        return _client().list_objects(
            object_type, properties=properties, limit=limit, after=after)

    @mcp.tool()
    def hubspot_create(
        object_type: str,
        properties: dict,
        associations: Optional[list[dict]] = None,
    ) -> dict:
        """Create a CRM object.

        Args:
            properties: object properties (e.g. {"email": …, "firstname": …} for a
                contact ; {"dealname": …, "amount": …} for a deal).
            associations: HubSpot v3 association objects (advanced).
        """
        return _client().create_object(
            object_type, properties, associations=associations)

    @mcp.tool()
    def hubspot_update(
        object_type: str, object_id: str, properties: dict,
    ) -> dict:
        """Update (PATCH) a CRM object's properties."""
        return _client().update_object(object_type, object_id, properties)

    @mcp.tool()
    def hubspot_delete(object_type: str, object_id: str) -> dict:
        """Archive a CRM object (moves it to HubSpot's recycle bin)."""
        return _client().delete_object(object_type, object_id)

    @mcp.tool()
    def hubspot_associations(
        object_type: str, object_id: str, to_object_type: str,
    ) -> dict:
        """List objects of `to_object_type` associated with an object.

        e.g. the deals of a contact: object_type="contacts", to_object_type="deals".
        """
        return _client().list_associations(object_type, object_id, to_object_type)

    @mcp.tool()
    def hubspot_create_note(
        body: str, object_type: str, object_id: str,
    ) -> dict:
        """Attach a note to a CRM object (contacts/companies/deals/tickets)."""
        return _client().create_note(body, object_type, object_id)

    @mcp.tool()
    def hubspot_owners() -> dict:
        """List HubSpot owners (users) — to assign records by ownerId."""
        return _client().list_owners()
