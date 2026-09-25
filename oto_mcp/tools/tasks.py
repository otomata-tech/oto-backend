"""Google Tasks — surface oto-core (TasksClient) exposée par-utilisateur, multi-compte.

Même substrat que Gmail : chaque user connecte un ou plusieurs comptes Google
sur `https://app.oto.ninja/` (flow OAuth unifié, scope `tasks` inclus). Les
tools `tasks_*` agissent sur le compte par défaut, ou sur le compte ciblé par
`account` (l'adresse email). Pas de clé plateforme : accès strictement per-user.

**Surface consolidée (ADR 0047 §Amendement, appliqué au produit tasks)** : un tool
par OBJET métier, le verbe en paramètre `op` — 6 tools → 2.
- `tasks_task` = **la tâche** : `list` / `get` / `upsert` (créer ou modifier) /
  `set_status` (fait / rouvert) / `rm` (supprimer). Tous ses ops partagent le même
  couple `(task_id, tasklist)` + `account` : recouvrement de paramètres maximal,
  c'est le critère de fusion.
- `tasks_lists` = **la liste de tâches**, et il reste SEUL : autre objet, et aucun
  paramètre commun avec la tâche (ni `task_id`, ni `tasklist` — c'est lui qui
  PRODUIT les ids de `tasklist` que l'autre consomme). Même cas que `zoho_modules`.

⚠️ Ce module ÉCRIT sur les données personnelles de l'utilisateur : `op="upsert"`
crée/modifie, `op="set_status"` modifie, **`op="rm"` supprime** (irréversible). Le
défaut `op="list"` est une LECTURE — un appel sans `op` n'écrit ni ne supprime jamais.
"""
from __future__ import annotations

import asyncio
from typing import Literal, Optional, get_args

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..auth import google as google_oauth

# Ops de `tasks_task`, et le libellé de refus qui les NOMME (source unique : un op
# ajouté ici doit apparaître dans le message, sinon l'agent ne peut pas se corriger).
# Le `Literal` est cette source : il sert à la fois d'annotation (⟹ `enum` au schéma
# JSON servi au modèle, qui contraint la génération) et de garde runtime via `get_args`.
_TaskOp = Literal["list", "get", "upsert", "set_status", "rm"]
_TASK_OPS = get_args(_TaskOp)
_UNKNOWN_OP = "op doit être 'list', 'get', 'upsert', 'set_status' ou 'rm'"


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback.

    La chaîne VIDE compte comme absente : `op='rm'` avec `task_id=""` partirait
    sinon taper l'API avec un id vide, et le refus doit venir d'ici, pas d'un 404
    amont opaque."""
    if value is None or value == "":
        raise _bad(f"op='{op}' requiert {name}")
    return value


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="tasks")
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.tasks.lib.tasks_client import TasksClient
    return TasksClient(credentials=creds)


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 lot 2 — voir gmail.py::_client_for_user_async pour la
# justification (même mécanisme de rafraîchissement de jeton, même méthode).
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google n'a pas répondu dans les {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(rafraîchissement de jeton) — réessaie.")


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

        Returns {tasklists: [{id, title, updated}], count} when listing. Use a
        list `id` as the `tasklist` argument of `tasks_task`; omit it for '@default'.

        Args:
            create: if given (a title), CREATE a new task list and return it
                instead of listing.
            account: email of the Google account to use (default if omitted).
        """
        client = await _client_for_user_async(account)
        if create:
            return await asyncio.to_thread(client.create_tasklist, create)
        tasklists = await asyncio.to_thread(client.list_tasklists)
        return {"tasklists": tasklists, "count": len(tasklists)}

    @mcp.tool()
    async def tasks_task(
        op: _TaskOp = "list",
        task_id: Optional[str] = None,
        tasklist: str = "@default",
        title: Optional[str] = None,
        notes: Optional[str] = None,
        due: Optional[str] = None,
        parent: Optional[str] = None,
        done: bool = True,
        completed: bool = False,
        max_results: int = 100,
        account: Optional[str] = None,
    ) -> dict:
        """A task inside a Google Tasks list — list, read, create/update, complete, delete.

        `op`:
        - **"list"** (default): list tasks in a task list.
        - **"get"**: get a single task by id.
        - **"upsert"**: create a task, or update an existing one.
        - **"set_status"**: complete (`done=True`) or reopen (`done=False`) a task.
        - **"rm"**: delete a task. Irreversible.

        Args:
            op: list (default) | get | upsert | set_status | rm.
            task_id: the task id — REQUIRED for op="get"/"set_status"/"rm". For
                op="upsert": when set, UPDATE that task instead of creating; pass
                any of title/notes/due to change.
            tasklist: task list id (default '@default'). Ids: `tasks_lists`.
            title: op="upsert" — task title. REQUIRED to create (omit `task_id`);
                optional to update.
            notes: op="upsert" — free-text notes.
            due: op="upsert" — due date, 'YYYY-MM-DD' or RFC 3339 (Tasks ignores
                the time).
            parent: op="upsert" — parent task id to nest under, same list (create only).
            done: op="set_status" — True = mark completed ; False = reopen (back to
                needsAction).
            completed: op="list" — include completed tasks (default false).
            max_results: op="list" — max tasks to return (default 100).
            account: email of the Google account to use (default if omitted).
        """
        # Refus AVANT toute résolution de credential : un op inconnu doit s'entendre
        # dire lesquels sont valides, pas « aucun compte Google connecté ».
        if op not in _TASK_OPS:
            raise _bad(_UNKNOWN_OP)

        client = await _client_for_user_async(account)

        if op == "list":
            tasks = await asyncio.to_thread(client.list_tasks, tasklist, completed, max_results)
            return {"tasks": tasks, "count": len(tasks)}
        if op == "get":
            return await asyncio.to_thread(
                client.get_task, _need(task_id, "task_id", op), tasklist
            )
        if op == "upsert":
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
        if op == "set_status":
            return await asyncio.to_thread(
                client.complete_task, _need(task_id, "task_id", op), tasklist, done
            )
        if op == "rm":
            return await asyncio.to_thread(
                client.delete_task, _need(task_id, "task_id", op), tasklist
            )
        raise _bad(_UNKNOWN_OP)   # inatteignable (garde en tête) — filet si `_TASK_OPS` grandit
