"""Folk CRM — groups, people, companies, deals, notes, interactions, reminders.

Wrappe `oto.tools.folk.FolkClient` (API publique https://developer.folk.app).
Clé résolue par appel via `access.resolve_api_key("folk")` — provider byo-only
(user key posée sur /account, ou credential partagé de l'org active). Pas de
clé plateforme.

Surface : lecture/écriture **par entité** (`folk_search`/`get`/`update`/`delete`
prennent `entity` = person|company[|deal]) — fusion sans perte. Les **créations**
restent des outils typés (`folk_create_*`) : leurs champs guident le modèle.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def register(mcp: FastMCP) -> None:
    from oto.tools.folk.client import FolkClient

    def _client() -> FolkClient:
        key, _ = access.resolve_api_key("folk", "FOLK_API_KEY")
        return FolkClient(api_key=key, field_filter=access.resolve_field_filter("folk"))

    # --- groups -------------------------------------------------------------

    @mcp.tool()
    async def folk_list_groups() -> dict:
        """List all groups in the Folk workspace (a group = a folder of people/companies/deals)."""
        return {"groups": _client().list_groups()}

    @mcp.tool()
    async def folk_group_custom_fields(group_id: str, entity_type: str = "person") -> dict:
        """List the custom fields defined on a group for an entity type.

        Args:
            group_id: Folk group ID.
            entity_type: "person" or "company".
        """
        return {"custom_fields": _client().get_group_custom_fields(group_id, entity_type)}

    # --- lecture/écriture par entité (person | company [| deal]) -------------

    @mcp.tool()
    async def folk_search(
        entity: str, filters: Optional[dict] = None, max_results: int = 100,
    ) -> dict:
        """Search Folk records. Fetches ALL matching pages — always pass filters on
        a large workspace.

        Args:
            entity: "person" or "company".
            filters: Field → value, matched with `like` (e.g. {"fullName": "Dupont",
                "emails": "@otomata.tech"} for people, {"name": "Otomata"} for companies).
            max_results: Truncate the response (default 100).
        """
        c = _client()
        if entity == "person":
            items = c.list_people(**(filters or {}))
        elif entity == "company":
            items = c.list_companies(**(filters or {}))
        else:
            raise _bad("entity doit être 'person' ou 'company'.")
        return {"entity": entity, "count": len(items), "results": items[:max_results]}

    @mcp.tool()
    async def folk_get(entity: str, id: str) -> dict:
        """Fetch a Folk record by ID (full record). `entity` = "person" or "company"."""
        c = _client()
        if entity == "person":
            return c.get_person(id)
        if entity == "company":
            return c.get_company(id)
        raise _bad("entity doit être 'person' ou 'company'.")

    @mcp.tool()
    async def folk_update(
        entity: str, id: str, fields: dict,
        group_id: Optional[str] = None, object_type: str = "deals",
    ) -> dict:
        """Update a Folk record (PATCH — only the given fields change).

        Args:
            entity: "person", "company" or "deal".
            id: the record ID (the deal_id for a deal).
            fields: Folk API field names, camelCase (e.g. {"jobTitle": "CTO"},
                {"industry": "SaaS"}, ou champs custom d'un deal).
            group_id: REQUIRED for `deal` (le groupe où vit le deal).
            object_type: nom de la collection (défaut "deals"), `deal` seulement.
        """
        c = _client()
        if entity == "person":
            return c.update_person(id, **fields)
        if entity == "company":
            return c.update_company(id, **fields)
        if entity == "deal":
            if not group_id:
                raise _bad("group_id requis pour entity='deal'.")
            return c.update_deal(group_id, id, object_type=object_type, **fields)
        raise _bad("entity doit être 'person', 'company' ou 'deal'.")

    @mcp.tool()
    async def folk_delete(entity: str, id: str) -> dict:
        """Delete a Folk record. Irreversible. `entity` = "person" or "company"."""
        c = _client()
        if entity == "person":
            return c.delete_person(id)
        if entity == "company":
            return c.delete_company(id)
        raise _bad("entity doit être 'person' ou 'company'.")

    # --- créations (typées : les champs guident le modèle) ------------------

    @mcp.tool()
    async def folk_create_person(
        first_name: str,
        last_name: Optional[str] = None,
        emails: Optional[list[str]] = None,
        phones: Optional[list[str]] = None,
        job_title: Optional[str] = None,
        company_name: Optional[str] = None,
        company_id: Optional[str] = None,
        group_ids: Optional[list[str]] = None,
    ) -> dict:
        """Create a person in Folk.

        Args:
            first_name: Required.
            company_id: Link to an existing company (takes precedence over company_name).
            company_name: Create/match a company by name.
            group_ids: Groups to add the person to.
        """
        return _client().create_person(
            first_name=first_name, last_name=last_name, emails=emails, phones=phones,
            job_title=job_title, company_name=company_name, company_id=company_id,
            group_ids=group_ids,
        )

    @mcp.tool()
    async def folk_create_company(
        name: str,
        emails: Optional[list[str]] = None,
        industry: Optional[str] = None,
    ) -> dict:
        """Create a company in Folk."""
        return _client().create_company(name=name, emails=emails, industry=industry)

    @mcp.tool()
    async def folk_create_deal(
        group_id: str,
        name: str,
        object_type: str = "deals",
        people_ids: Optional[list[str]] = None,
        company_ids: Optional[list[str]] = None,
        custom_fields: Optional[dict] = None,
    ) -> dict:
        """Create a deal in a Folk group, optionally linked to people/companies.

        Args:
            custom_fields: Custom field values keyed by field name (e.g. status, amount).
        """
        return _client().create_deal(
            group_id, name, object_type=object_type, people_ids=people_ids,
            company_ids=company_ids, custom_fields=custom_fields,
        )

    @mcp.tool()
    async def folk_create_note(
        entity_id: str, content: str, visibility: str = "public"
    ) -> dict:
        """Attach a note to a Folk entity (person/company/deal).

        Args:
            visibility: "public" (whole workspace) or "private".
        """
        return _client().create_note(entity_id, content, visibility=visibility)

    @mcp.tool()
    async def folk_create_interaction(
        entity_id: str,
        type: str,
        title: str,
        content: Optional[str] = None,
        date_time: Optional[str] = None,
    ) -> dict:
        """Log an interaction (call, meeting, email…) on a Folk entity.

        Args:
            type: Interaction type as defined in the workspace (e.g. "call", "meeting").
            date_time: ISO 8601 (defaults to now server-side).
        """
        return _client().create_interaction(
            entity_id, type=type, title=title, content=content, date_time=date_time,
        )

    @mcp.tool()
    async def folk_create_reminder(
        entity_id: str, name: str, recurrence_rule: str, visibility: str = "public"
    ) -> dict:
        """Create a reminder on a Folk entity.

        Args:
            recurrence_rule: iCal RRULE (e.g. "DTSTART:20260701T090000Z\\nRRULE:FREQ=WEEKLY").
        """
        return _client().create_reminder(
            entity_id, name, recurrence_rule=recurrence_rule, visibility=visibility,
        )

    # --- lists (énumération par collection) ---------------------------------

    @mcp.tool()
    async def folk_list_deals(group_id: str, object_type: str = "deals") -> dict:
        """List the deals (or another custom object) of a Folk group.

        Args:
            group_id: Folk group ID (see folk_list_groups).
            object_type: Custom-object collection name (default "deals").
        """
        return {"deals": _client().list_deals(group_id, object_type=object_type)}

    @mcp.tool()
    async def folk_list_notes(entity_id: Optional[str] = None) -> dict:
        """List notes, optionally filtered to one entity (person/company/deal ID)."""
        return {"notes": _client().list_notes(entity_id=entity_id)}

    @mcp.tool()
    async def folk_list_reminders(entity_id: Optional[str] = None) -> dict:
        """List reminders, optionally filtered to one entity."""
        return {"reminders": _client().list_reminders(entity_id=entity_id)}
