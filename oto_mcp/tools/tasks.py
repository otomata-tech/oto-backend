"""Google Tasks — surface oto-core (TasksClient) exposée par-utilisateur, multi-compte.

Même substrat que Gmail : chaque user connecte un ou plusieurs comptes Google
sur `https://manage.oto.cx/` (flow OAuth unifié, scope `tasks` inclus). Les
tools `tasks_*` agissent sur le compte par défaut, ou sur le compte ciblé par
`account` (l'adresse email). Pas de clé plateforme : accès strictement per-user.

Surface regroupée (6 tools) : gérer les listes, lister/lire les tâches,
**upsert** (créer ou modifier), changer le **statut** (fait/rouvert), supprimer.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, google_oauth


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise _bad(str(e))
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
    async def tasks_lists(create: Optional[str] = None, account: Optional[str] = None) -> dict:
        """List the user's Google Tasks lists — or create one.

        Args:
            create: if given (a title), CREATE a new task list and return it
                instead of listing.
            account: email of the Google account to use (default if omitted).

        Returns {tasklists: [{id, title, updated}], count} when listing. Use a
        list `id` as the `tasklist` argument of the other tasks_* tools; omit it
        for '@default'.
        """
        client = _client_for_user(account)
        if create:
            return await asyncio.to_thread(client.create_tasklist, create)
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
        """
        client = _client_for_user(account)
        tasks = await asyncio.to_thread(client.list_tasks, tasklist, completed, max_results)
        return {"tasks": tasks, "count": len(tasks)}

    @mcp.tool()
    async def tasks_get(task_id: str, tasklist: str = "@default", account: Optional[str] = None) -> dict:
        """Get a single task by id."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.get_task, task_id, tasklist)

    @mcp.tool()
    async def tasks_upsert(
        title: Optional[str] = None,
        task_id: Optional[str] = None,
        notes: Optional[str] = None,
        due: Optional[str] = None,
        tasklist: str = "@default",
        parent: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Create a task, or update an existing one.

        Args:
            title: task title. REQUIRED to create (omit `task_id`); optional to update.
            task_id: when set, UPDATE that task instead of creating; pass any of
                title/notes/due to change.
            notes: free-text notes.
            due: due date, 'YYYY-MM-DD' or RFC 3339 (Tasks ignores the time).
            tasklist: task list id (default '@default').
            parent: parent task id to nest under, same list (create only).
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        if task_id:
            if title is None and notes is None and due is None:
                raise _bad("Pour une mise à jour, fournis title, notes ou due.")
            return await asyncio.to_thread(
                client.update_task, task_id, tasklist, title, notes, _normalize_due(due)
            )
        if not title:
            raise _bad("`title` requis pour créer une tâche (ou fournis `task_id` pour modifier).")
        return await asyncio.to_thread(
            client.create_task, title, notes, _normalize_due(due), tasklist, parent
        )

    @mcp.tool()
    async def tasks_set_status(
        task_id: str, done: bool = True, tasklist: str = "@default", account: Optional[str] = None,
    ) -> dict:
        """Complete (done=True) or reopen (done=False) a task.

        Args:
            task_id: the task id.
            done: True = mark completed ; False = reopen (back to needsAction).
            tasklist: task list id (default '@default').
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.complete_task, task_id, tasklist, done)

    @mcp.tool()
    async def tasks_rm(task_id: str, tasklist: str = "@default", account: Optional[str] = None) -> dict:
        """Delete a task."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.delete_task, task_id, tasklist)
