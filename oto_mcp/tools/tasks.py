"""Google Tasks — surface oto-cli (TasksClient) exposée par-utilisateur, multi-compte.

Même substrat que Gmail/Datastore : chaque user connecte un ou plusieurs comptes
Google sur `https://app.oto.ninja/` (flow OAuth unifié, scope `tasks` inclus).
Les tools `tasks_*` agissent sur le compte par défaut, ou sur le compte ciblé
par le paramètre `account` (l'adresse email). Pas de clé plateforme : accès
strictement per-user via OAuth.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, google_oauth


def _client_for_user(account: Optional[str] = None):
    """Instancie un TasksClient oto-cli avec les credentials du user.

    `account` (email) cible un compte précis ; None = compte par défaut.
    Lève une McpError actionnable si aucun compte Google n'est connecté.
    """
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    from oto.tools.google.tasks.lib.tasks_client import TasksClient
    return TasksClient(credentials=creds)


def _normalize_due(due: Optional[str]) -> Optional[str]:
    """Expand a YYYY-MM-DD date to the RFC 3339 the Tasks API wants."""
    if due is None:
        return None
    if len(due) == 10 and due[4] == '-' and due[7] == '-':
        return f"{due}T00:00:00.000Z"
    return due


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def tasks_lists(account: Optional[str] = None) -> dict:
        """List the user's Google Tasks task lists.

        Args:
            account: email of the Google account to use (default if omitted).

        Returns {tasklists: [{id, title, updated}]}. Use an `id` as the
        `tasklist` argument of the other tasks_* tools; omit it for '@default'.
        """
        client = _client_for_user(account)
        tasklists = await asyncio.to_thread(client.list_tasklists)
        return {"tasklists": tasklists, "count": len(tasklists)}

    @mcp.tool()
    async def tasks_list(
        tasklist: str = "@default",
        completed: bool = False,
        max_results: int = 100,
        account: Optional[str] = None,
    ) -> dict:
        """List tasks in a task list.

        Args:
            tasklist: task list id (default '@default').
            completed: include completed tasks (default false).
            max_results: max tasks to return (default 100).
            account: email of the Google account to use (default if omitted).

        Returns {tasks: [{id, title, status, due, completed, updated, notes?, parent?}]}.
        """
        client = _client_for_user(account)
        tasks = await asyncio.to_thread(client.list_tasks, tasklist, completed, max_results)
        return {"tasks": tasks, "count": len(tasks)}

    @mcp.tool()
    async def tasks_get(task_id: str, tasklist: str = "@default", account: Optional[str] = None) -> dict:
        """Fetch a single task (full detail).

        Args:
            task_id: the task id.
            tasklist: task list id (default '@default').
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.get_task, task_id, tasklist)

    @mcp.tool()
    async def tasks_add(
        title: str,
        notes: Optional[str] = None,
        due: Optional[str] = None,
        tasklist: str = "@default",
        parent: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Add a task.

        Args:
            title: task title.
            notes: free-text notes (optional).
            due: due date, 'YYYY-MM-DD' or RFC 3339 (optional; Tasks ignores the time).
            tasklist: task list id (default '@default').
            parent: parent task id to nest under, same list (optional).
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(
            client.create_task, title, notes, _normalize_due(due), tasklist, parent
        )

    @mcp.tool()
    async def tasks_update(
        task_id: str,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        due: Optional[str] = None,
        tasklist: str = "@default",
        account: Optional[str] = None,
    ) -> dict:
        """Update a task's title, notes and/or due date.

        Args:
            task_id: the task id.
            title: new title (optional).
            notes: new notes (optional).
            due: new due date, 'YYYY-MM-DD' or RFC 3339 (optional).
            tasklist: task list id (default '@default').
            account: email of the Google account to use (default if omitted).
        """
        if title is None and notes is None and due is None:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Provide title, notes or due to update."))
        client = _client_for_user(account)
        return await asyncio.to_thread(
            client.update_task, task_id, tasklist, title, notes, _normalize_due(due)
        )

    @mcp.tool()
    async def tasks_done(task_id: str, tasklist: str = "@default", account: Optional[str] = None) -> dict:
        """Mark a task as completed.

        Args:
            task_id: the task id.
            tasklist: task list id (default '@default').
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.complete_task, task_id, tasklist, True)

    @mcp.tool()
    async def tasks_reopen(task_id: str, tasklist: str = "@default", account: Optional[str] = None) -> dict:
        """Reopen a completed task (back to needsAction).

        Args:
            task_id: the task id.
            tasklist: task list id (default '@default').
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.complete_task, task_id, tasklist, False)

    @mcp.tool()
    async def tasks_rm(task_id: str, tasklist: str = "@default", account: Optional[str] = None) -> dict:
        """Delete a task.

        Args:
            task_id: the task id.
            tasklist: task list id (default '@default').
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.delete_task, task_id, tasklist)

    @mcp.tool()
    async def tasks_add_list(title: str, account: Optional[str] = None) -> dict:
        """Create a new task list.

        Args:
            title: the task list title.
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.create_tasklist, title)
