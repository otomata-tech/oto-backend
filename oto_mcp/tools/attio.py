"""Attio CRM — lecture du CRM + création de notes/tasks.

Pas d'écriture destructive (pas de update/delete) : le scope MCP s'arrête à
la lecture + ajout de contexte. Pour modifier ou supprimer des records,
passer par l'UI Attio.

Clé résolue par appel via `access.resolve_api_key("attio")`. Comme Attio
n'a pas de quota par défaut (cf. `access._QUOTA_DEFAULTS`), seuls les
admins (avec une `ATTIO_API_KEY` serveur) ou les users avec leur propre
clé posée sur `/account` peuvent appeler ces tools.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.attio.client import AttioClient

    def _client() -> tuple[AttioClient, bool]:
        key, is_platform = access.resolve_api_key("attio", "ATTIO_API_KEY")
        return AttioClient(api_key=key), is_platform

    def _record_if_platform(is_platform: bool) -> None:
        if is_platform:
            access.record_platform_usage("attio")

    # --- companies / people / deals : list / get / search ------------------

    @mcp.tool()
    async def attio_list_companies(limit: int = 50, offset: int = 0) -> dict:
        """List companies in the Attio CRM workspace.

        Args:
            limit: Max records (default 50).
            offset: Pagination offset.
        """
        client, is_platform = _client()
        result = client.companies.list(limit=limit, offset=offset)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_company(record_id: str) -> dict:
        """Fetch a company record by its Attio record ID."""
        client, is_platform = _client()
        result = client.companies.get(record_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_search_companies(query: str, limit: int = 50) -> dict:
        """Search companies by free-text query (matches name/domain/etc.)."""
        client, is_platform = _client()
        result = client.companies.search(query=query, limit=limit)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_company(attributes: dict) -> dict:
        """Create a company record.

        Args:
            attributes: Attio attribute dict — keys are slugs of the
                workspace's company object (`name`, `domains`, `description`,
                `categories`, etc.). Each value follows Attio's value format
                (typically a list, e.g. `{"name": [{"value": "Acme"}]}`).
        """
        client, is_platform = _client()
        result = client.companies.create(**attributes)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_people(limit: int = 50, offset: int = 0) -> dict:
        """List people in the Attio CRM workspace."""
        client, is_platform = _client()
        result = client.people.list(limit=limit, offset=offset)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_person(record_id: str) -> dict:
        """Fetch a person record by its Attio record ID."""
        client, is_platform = _client()
        result = client.people.get(record_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_search_people(query: str, limit: int = 50) -> dict:
        """Search people by free-text query (matches name/email/etc.)."""
        client, is_platform = _client()
        result = client.people.search(query=query, limit=limit)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_person(attributes: dict) -> dict:
        """Create a person record.

        Args:
            attributes: Attio attribute dict (`name`, `email_addresses`,
                `phone_numbers`, `company`, `job_title`, etc.). Values follow
                Attio's format (typically lists of value objects).
        """
        client, is_platform = _client()
        result = client.people.create(**attributes)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_deals(limit: int = 50, offset: int = 0) -> dict:
        """List deals in the Attio CRM workspace."""
        client, is_platform = _client()
        result = client.deals.list(limit=limit, offset=offset)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_deal(record_id: str) -> dict:
        """Fetch a deal record by its Attio record ID."""
        client, is_platform = _client()
        result = client.deals.get(record_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_search_deals(query: str, limit: int = 50) -> dict:
        """Search deals by free-text query."""
        client, is_platform = _client()
        result = client.deals.search(query=query, limit=limit)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_deal(attributes: dict) -> dict:
        """Create a deal record.

        Args:
            attributes: Attio attribute dict (`name`, `value`, `stage`,
                `associated_company`, `owner`, etc.).
        """
        client, is_platform = _client()
        result = client.deals.create(**attributes)
        _record_if_platform(is_platform)
        return result

    # --- notes / tasks : list + create (pas de update/delete) --------------

    @mcp.tool()
    async def attio_list_notes(
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
    ) -> dict:
        """List notes — optionally scoped to a parent record.

        Args:
            parent_object: companies | people | deals (optional).
            parent_record_id: record ID under that object (optional).
        """
        client, is_platform = _client()
        result = client.notes.list(
            parent_object=parent_object, parent_record_id=parent_record_id,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_note(
        parent_object: str,
        parent_record_id: str,
        title: str,
        content: str,
    ) -> dict:
        """Create a note attached to a record.

        Args:
            parent_object: companies | people | deals.
            parent_record_id: Attio record ID to attach the note to.
            title: Note title.
            content: Markdown body.
        """
        client, is_platform = _client()
        result = client.notes.create(
            parent_object=parent_object,
            parent_record_id=parent_record_id,
            title=title,
            content=content,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_tasks(completed: Optional[bool] = None) -> dict:
        """List tasks — optionally filtered by completion status."""
        client, is_platform = _client()
        result = client.tasks.list(completed=completed)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_task(
        content: str,
        deadline: Optional[str] = None,
        linked_object: Optional[str] = None,
        linked_record_id: Optional[str] = None,
    ) -> dict:
        """Create a task, optionally linked to a record.

        Args:
            content: Task description (max 2000 chars).
            deadline: ISO datetime or YYYY-MM-DD.
            linked_object: companies | people (optional).
            linked_record_id: record ID under that object (optional).
        """
        client, is_platform = _client()
        result = client.tasks.create(
            content=content,
            deadline=deadline,
            linked_object=linked_object,
            linked_record_id=linked_record_id,
        )
        _record_if_platform(is_platform)
        return result
