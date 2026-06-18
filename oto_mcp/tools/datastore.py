"""Datastore — stockage de données structurées légères par user (PG natif, ADR 0016).

Chaque user a son propre set de "namespaces". Schéma libre : chaque row = un
dict JSON (stocké en JSONB, types préservés), les champs apparaissent au fur et
à mesure. Trois champs auto-managés exposés à plat : `_id`, `_created_at`,
`_updated_at`. Aucune dépendance externe — surface plateforme self-contained.

Surface (« moins d'outils, plus d'args ») : `data_write`/`data_rows`/`data_share`
fondent append↔update / get↔list / share↔unshare via un arg de mode. Les
destructifs (delete_namespace, delete_row) et la création restent séparés.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db
from ..datastore import (
    NamespaceExists,
    NamespaceNotFound,
    NamespaceReadOnly,
    RowNotFound,
    make_store,
)


def _store_for(sub: str):
    return make_store(sub)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def data_list_namespaces() -> dict:
        """List the user's datastore namespaces (owned + shared)."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        return {"namespaces": store.list_namespaces()}

    @mcp.tool()
    async def data_create_namespace(namespace: str) -> dict:
        """Create a new datastore namespace (PG-backed, schema-free).

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
        """Delete a namespace and all its rows (irreversible). Owner only."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            store.delete_namespace(namespace)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        return {"ok": True, "namespace": namespace}

    @mcp.tool()
    async def data_write(namespace: str, row: dict, id: str | None = None) -> dict:
        """Write a row. WITHOUT `id` = append a NEW row (new JSON keys auto-create
        columns). WITH `id` = PARTIAL update of that row (only provided fields
        change). Returns the row (with `_id`/`_created_at`/`_updated_at`).

        Args:
            namespace: target namespace.
            row: row content as a dict (strings/numbers/bools/objects/arrays,
                JSON-encoded automatically).
            id: omit = append a new row ; provided = partial update of that `_id`.
        """
        sub = access.current_user_sub_or_raise()
        if not isinstance(row, dict):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="row doit être un dict"))
        store = _store_for(sub)
        try:
            return store.append_row(namespace, row) if id is None \
                else store.update_row(namespace, id, row)
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` partagé en lecture seule"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))

    @mcp.tool()
    async def data_rows(
        namespace: str, id: str | None = None,
        filter: Optional[dict] = None, limit: int = 100,
    ) -> dict:
        """Read rows. WITH `id` = the single row (by `_id`). WITHOUT `id` = list
        rows (optional exact-match `filter`, `limit`).

        Args:
            namespace: target namespace.
            id: `_id` of one row ; omit = list rows.
            filter: dict `{column: value}` exact match (list mode only),
                e.g. `{"project": "roundtable"}`.
            limit: max rows (default 100, list mode only).
        """
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            if id is not None:
                return store.get_row(namespace, id)
            rows = store.list_rows(namespace, filter=filter, limit=limit)
            return {"rows": rows, "count": len(rows)}
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
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` partagé en lecture seule"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))
        return {"ok": True, "id": id}

    @mcp.tool()
    async def data_url(namespace: str) -> dict:
        """Return the dashboard URL of a namespace (for the user to open/edit in browser)."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        try:
            return {"url": store.get_url(namespace)}
        except NamespaceNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` inconnu"))

    @mcp.tool()
    async def data_share(
        namespace: str, email: str, permission: str = "write", remove: bool = False,
    ) -> dict:
        """Share (or with `remove=True`, unshare) a namespace with another oto user
        (by email). The recipient accesses it with their own oto account.

        Args:
            namespace: namespace to (un)share (must be owned by you).
            email: email of the recipient oto user.
            permission: 'read' or 'write' (default write) — when sharing.
            remove: True = revoke access instead of granting it.
        """
        sub = access.current_user_sub_or_raise()
        recipient = db.get_user_by_email(email)
        if not recipient:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"aucun utilisateur oto avec l'email {email}"))

        if remove:
            removed = db.unshare_datastore_namespace(sub, namespace, recipient["sub"])
            if not removed:
                raise McpError(ErrorData(code=INVALID_PARAMS,
                                         message=f"pas de partage actif pour {email} sur {namespace}"))
            return {"ok": True, "namespace": namespace, "unshared_with": email}

        if permission not in ("read", "write"):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="permission must be 'read' or 'write'"))
        if not db.get_datastore_namespace(sub, namespace):
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` not found"))
        try:
            db.share_datastore_namespace(sub, namespace, recipient["sub"], permission)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        return {"ok": True, "namespace": namespace, "shared_with": email, "permission": permission}
