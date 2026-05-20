"""Datastore — stockage de données structurées légères par user (Google Sheets).

Chaque user a son propre set de "namespaces" (= un Google Sheet chacun) dans
son Drive. Schéma libre : chaque row = un dict JSON, les colonnes
apparaissent au fur et à mesure. Les 3 premières colonnes sont auto-managées
(`_id`, `_created_at`, `_updated_at`).

Prérequis user : avoir connecté son compte Google Drive sur
`https://oto.ninja/account` (section Datastore) — sinon les tools lèvent
une McpError actionnable.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..datastore import (
    GoogleNotConnected,
    NamespaceExists,
    NamespaceNotFound,
    RowNotFound,
    make_store,
)


def _store_for(sub: str):
    try:
        return make_store(sub)
    except GoogleNotConnected as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def data_list_namespaces() -> dict:
        """List the user's datastore namespaces (Google Sheets)."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        return {"namespaces": store.list_namespaces()}

    @mcp.tool()
    async def data_create_namespace(namespace: str) -> dict:
        """Create a new datastore namespace. Provisions a Google Sheet
        named `oto.<namespace>` in the user's Drive.

        Args:
            namespace: kebab-case identifier, unique per user (e.g. `timetrack`).
        """
        sub = access.current_user_sub_or_raise()
        if not namespace or not namespace.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="namespace requis"))
        store = _store_for(sub)
        try:
            return store.create_namespace(namespace.strip())
        except NamespaceExists:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"namespace `{namespace}` existe déjà",
            ))

    @mcp.tool()
    async def data_delete_namespace(namespace: str) -> dict:
        """Delete a namespace. Trashes the Google Sheet (recoverable for 30j
        from the Drive trash) and removes the registration.
        """
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            store.delete_namespace(namespace)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        return {"ok": True, "namespace": namespace}

    @mcp.tool()
    async def data_append(namespace: str, row: dict) -> dict:
        """Append a row to a namespace. New JSON keys auto-create columns.
        Returns the created row including auto-generated `_id`,
        `_created_at`, `_updated_at`.

        Args:
            namespace: target namespace.
            row: row content as a dict. Values can be strings, numbers, bools,
                or nested objects/arrays (JSON-encoded automatically).
        """
        sub = access.current_user_sub_or_raise()
        if not isinstance(row, dict):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="row doit être un dict"))
        store = _store_for(sub)
        try:
            return store.append_row(namespace, row)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))

    @mcp.tool()
    async def data_get(namespace: str, id: str) -> dict:
        """Fetch a single row by its `_id`."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            return store.get_row(namespace, id)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))

    @mcp.tool()
    async def data_list(
        namespace: str,
        filter: Optional[dict] = None,
        limit: int = 100,
    ) -> dict:
        """List rows in a namespace. Optional exact-match filter on any column.

        Args:
            namespace: target namespace.
            filter: dict of `{column: value}` — exact match on stringified
                values (e.g. `{"project": "roundtable"}`).
            limit: max rows to return (default 100).
        """
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            rows = store.list_rows(namespace, filter=filter, limit=limit)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        return {"rows": rows, "count": len(rows)}

    @mcp.tool()
    async def data_update(namespace: str, id: str, patch: dict) -> dict:
        """Partial update of a row by `_id`. Only provided fields are changed.
        `_updated_at` is auto-bumped.
        """
        sub = access.current_user_sub_or_raise()
        if not isinstance(patch, dict):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="patch doit être un dict"))
        store = _store_for(sub)
        try:
            return store.update_row(namespace, id, patch)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))

    @mcp.tool()
    async def data_delete_row(namespace: str, id: str) -> dict:
        """Delete a row by `_id`."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            store.delete_row(namespace, id)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))
        return {"ok": True, "id": id}

    @mcp.tool()
    async def data_url(namespace: str) -> dict:
        """Return the Google Sheets web URL of a namespace (for the user to open in browser)."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            return {"url": store.get_url(namespace)}
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
