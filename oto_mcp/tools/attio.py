"""Attio CRM — CRUD complet records + notes/tasks.

Couvre create/read/update/delete sur companies, people, deals, et
create/list/delete pour notes (l'API Attio ne permet pas d'éditer le corps
d'une note), et create/list/update/delete pour tasks (update limité à
`deadline_at`, `is_completed`, `linked_records`, `assignees` côté API).

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
    async def attio_update_company(record_id: str, attributes: dict) -> dict:
        """Update a company record (PATCH — multiselect values are appended).

        Args:
            record_id: Attio company record ID.
            attributes: Attribute slugs → value(s), Attio value format.
        """
        client, is_platform = _client()
        result = client.companies.update(record_id, **attributes)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_delete_company(record_id: str) -> dict:
        """Delete a company record by ID. Irreversible."""
        client, is_platform = _client()
        result = client.companies.delete(record_id)
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
    async def attio_update_person(record_id: str, attributes: dict) -> dict:
        """Update a person record (PATCH — multiselect values are appended).

        Args:
            record_id: Attio person record ID.
            attributes: Attribute slugs → value(s), Attio value format.
        """
        client, is_platform = _client()
        result = client.people.update(record_id, **attributes)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_delete_person(record_id: str) -> dict:
        """Delete a person record by ID. Irreversible."""
        client, is_platform = _client()
        result = client.people.delete(record_id)
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

    @mcp.tool()
    async def attio_update_deal(record_id: str, attributes: dict) -> dict:
        """Update a deal record (PATCH — multiselect values are appended).

        Args:
            record_id: Attio deal record ID.
            attributes: Attribute slugs → value(s), Attio value format.
        """
        client, is_platform = _client()
        result = client.deals.update(record_id, **attributes)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_delete_deal(record_id: str) -> dict:
        """Delete a deal record by ID. Irreversible."""
        client, is_platform = _client()
        result = client.deals.delete(record_id)
        _record_if_platform(is_platform)
        return result

    # --- notes / tasks : list + create + delete (+ update tasks) -----------

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
    async def attio_get_note(note_id: str) -> dict:
        """Get a single note by ID (including markdown content)."""
        client, is_platform = _client()
        result = client.notes.get(note_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_delete_note(note_id: str) -> dict:
        """Delete a note by ID. Irreversible. Attio API does not support editing note body."""
        client, is_platform = _client()
        result = client.notes.delete(note_id)
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

    @mcp.tool()
    async def attio_update_task(
        task_id: str,
        deadline: Optional[str] = None,
        is_completed: Optional[bool] = None,
        assignee_id: Optional[str] = None,
        linked_object: Optional[str] = None,
        linked_record_id: Optional[str] = None,
    ) -> dict:
        """Update a task. Attio API only allows changing deadline_at, is_completed, assignees, linked_records.

        Args:
            task_id: Attio task ID.
            deadline: ISO datetime or YYYY-MM-DD (optional).
            is_completed: Mark as done/not done (optional).
            assignee_id: Workspace member ID (optional).
            linked_object: companies | people | deals — pair with linked_record_id.
            linked_record_id: Record ID under that object.
        """
        client, is_platform = _client()
        result = client.tasks.update(
            task_id,
            deadline=deadline,
            is_completed=is_completed,
            assignee_id=assignee_id,
            linked_object=linked_object,
            linked_record_id=linked_record_id,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_task(task_id: str) -> dict:
        """Get a single task by ID."""
        client, is_platform = _client()
        result = client.tasks.get(task_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_delete_task(task_id: str) -> dict:
        """Delete a task by ID. Irreversible."""
        client, is_platform = _client()
        result = client.tasks.delete(task_id)
        _record_if_platform(is_platform)
        return result

    # --- lists ------------------------------------------------------------

    @mcp.tool()
    async def attio_list_lists() -> dict:
        """List all Attio lists accessible to the token."""
        client, is_platform = _client()
        result = client.lists.list()
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_list(list_id_or_slug: str) -> dict:
        """Get a single list by ID or slug."""
        client, is_platform = _client()
        result = client.lists.get(list_id_or_slug)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_list(
        name: str,
        parent_object: str,
        api_slug: Optional[str] = None,
        workspace_access: str = "full-access",
    ) -> dict:
        """Create a new list.

        Args:
            name: Display name.
            parent_object: Object slug the list targets (companies | people | deals | custom).
            api_slug: Optional API slug (auto-derived if omitted).
            workspace_access: full-access | read-and-write | read-only.
        """
        client, is_platform = _client()
        result = client.lists.create(
            name=name,
            parent_object=parent_object,
            api_slug=api_slug,
            workspace_access=workspace_access,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_update_list(list_id_or_slug: str, attributes: dict) -> dict:
        """Update an existing list (name, api_slug, access controls)."""
        client, is_platform = _client()
        result = client.lists.update(list_id_or_slug, **attributes)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_list_views(list_id_or_slug: str) -> dict:
        """List saved views for a list."""
        client, is_platform = _client()
        result = client.lists.views(list_id_or_slug)
        _record_if_platform(is_platform)
        return result

    # --- entries (list membership) ----------------------------------------

    @mcp.tool()
    async def attio_query_entries(
        list_id_or_slug: str,
        filter: Optional[dict] = None,
        sorts: Optional[list] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Query entries in a list with optional filter/sort.

        Args:
            list_id_or_slug: The target list.
            filter: Attio filter object (e.g. `{"name": "Acme"}`).
            sorts: List of sort dicts.
        """
        client, is_platform = _client()
        result = client.entries.query(
            list_id_or_slug, filter=filter, sorts=sorts, limit=limit, offset=offset,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_entry(list_id_or_slug: str, entry_id: str) -> dict:
        """Get a single list entry by ID."""
        client, is_platform = _client()
        result = client.entries.get(list_id_or_slug, entry_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_entry(
        list_id_or_slug: str,
        parent_record_id: str,
        parent_object: str,
        entry_values: Optional[dict] = None,
    ) -> dict:
        """Add a record to a list as a new entry.

        Args:
            list_id_or_slug: The target list.
            parent_record_id: ID of the record (company/person/deal) to add.
            parent_object: companies | people | deals | custom slug.
            entry_values: Optional list-specific attribute values.
        """
        client, is_platform = _client()
        result = client.entries.create(
            list_id_or_slug,
            parent_record_id=parent_record_id,
            parent_object=parent_object,
            entry_values=entry_values,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_update_entry(
        list_id_or_slug: str,
        entry_id: str,
        entry_values: dict,
        overwrite_multiselect: bool = False,
    ) -> dict:
        """Update list entry values. PATCH appends multiselect by default; pass overwrite_multiselect=True for PUT."""
        client, is_platform = _client()
        result = client.entries.update(
            list_id_or_slug,
            entry_id,
            entry_values=entry_values,
            overwrite_multiselect=overwrite_multiselect,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_delete_entry(list_id_or_slug: str, entry_id: str) -> dict:
        """Remove a record from a list by deleting its entry. Irreversible."""
        client, is_platform = _client()
        result = client.entries.delete(list_id_or_slug, entry_id)
        _record_if_platform(is_platform)
        return result

    # --- workspace members ------------------------------------------------

    @mcp.tool()
    async def attio_list_workspace_members() -> dict:
        """List all workspace members (humans with access to the workspace)."""
        client, is_platform = _client()
        result = client.workspace_members.list()
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_workspace_member(workspace_member_id: str) -> dict:
        """Get a single workspace member by ID."""
        client, is_platform = _client()
        result = client.workspace_members.get(workspace_member_id)
        _record_if_platform(is_platform)
        return result

    # --- comments / threads -----------------------------------------------

    @mcp.tool()
    async def attio_list_threads(
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        list_id: Optional[str] = None,
        entry_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List comment threads, optionally filtered by parent record or list entry."""
        client, is_platform = _client()
        result = client.threads.list(
            parent_object=parent_object,
            parent_record_id=parent_record_id,
            list_id=list_id,
            entry_id=entry_id,
            limit=limit,
            offset=offset,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_thread(thread_id: str) -> dict:
        """Get a thread with all its comments."""
        client, is_platform = _client()
        result = client.threads.get(thread_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_comment(comment_id: str) -> dict:
        """Get a single comment by ID."""
        client, is_platform = _client()
        result = client.comments.get(comment_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_create_comment(
        content: str,
        author_id: str,
        thread_id: Optional[str] = None,
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        list_id: Optional[str] = None,
        entry_id: Optional[str] = None,
    ) -> dict:
        """Create a comment — either replying in a thread or starting one on a record/entry.

        Provide one of: thread_id, (parent_object + parent_record_id), or (list_id + entry_id).
        author_id is a workspace_member_id.
        """
        client, is_platform = _client()
        result = client.comments.create(
            content=content,
            author_id=author_id,
            thread_id=thread_id,
            parent_object=parent_object,
            parent_record_id=parent_record_id,
            list_id=list_id,
            entry_id=entry_id,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_delete_comment(comment_id: str) -> dict:
        """Delete a comment. If it heads a thread, the whole thread is deleted."""
        client, is_platform = _client()
        result = client.comments.delete(comment_id)
        _record_if_platform(is_platform)
        return result

    # --- meetings / call recordings / transcripts -------------------------

    @mcp.tool()
    async def attio_list_meetings(limit: int = 50, offset: int = 0) -> dict:
        """List meetings (calendar events synced into Attio)."""
        client, is_platform = _client()
        result = client.meetings.list(limit=limit, offset=offset)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_meeting(meeting_id: str) -> dict:
        """Get a single meeting by ID."""
        client, is_platform = _client()
        result = client.meetings.get(meeting_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_call_recordings(meeting_id: str) -> dict:
        """List call recordings for a meeting."""
        client, is_platform = _client()
        result = client.call_recordings.list(meeting_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_call_recording(meeting_id: str, call_recording_id: str) -> dict:
        """Get a single call recording by ID."""
        client, is_platform = _client()
        result = client.call_recordings.get(meeting_id, call_recording_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_call_transcript(meeting_id: str, call_recording_id: str) -> dict:
        """Get the transcript text for a call recording."""
        client, is_platform = _client()
        result = client.call_recordings.transcript(meeting_id, call_recording_id)
        _record_if_platform(is_platform)
        return result

    # --- meta (objects + attributes) --------------------------------------

    @mcp.tool()
    async def attio_list_objects() -> dict:
        """List all objects (system + custom) defined in the workspace.

        Useful for an LLM to discover what record types exist beyond the
        standard companies/people/deals (e.g. custom objects like "products").
        """
        client, is_platform = _client()
        result = client.objects.list()
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_object(object_id_or_slug: str) -> dict:
        """Get a single object definition by ID or slug."""
        client, is_platform = _client()
        result = client.objects.get(object_id_or_slug)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_object_views(object_id_or_slug: str) -> dict:
        """List saved views for an object."""
        client, is_platform = _client()
        result = client.objects.views(object_id_or_slug)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_attributes(target: str, identifier: str) -> dict:
        """List attributes (schema) on an object or list.

        Args:
            target: "objects" or "lists".
            identifier: object/list ID or slug (e.g. "companies").
        """
        client, is_platform = _client()
        result = client.attributes.list(target, identifier)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_get_attribute(target: str, identifier: str, attribute: str) -> dict:
        """Get a single attribute definition."""
        client, is_platform = _client()
        result = client.attributes.get(target, identifier, attribute)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_attribute_options(target: str, identifier: str, attribute: str) -> dict:
        """List the select options for a select-type attribute."""
        client, is_platform = _client()
        result = client.attributes.options(target, identifier, attribute)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def attio_list_attribute_statuses(target: str, identifier: str, attribute: str) -> dict:
        """List the statuses for a status-type attribute."""
        client, is_platform = _client()
        result = client.attributes.statuses(target, identifier, attribute)
        _record_if_platform(is_platform)
        return result
