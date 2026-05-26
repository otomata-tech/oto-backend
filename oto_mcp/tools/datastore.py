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

from .. import access, db
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

    def _drive_share(sub: str, spreadsheet_id: str, email: str, role: str) -> None:
        """Share a Google Sheet via Drive API using the owner's credentials."""
        from .. import google_oauth
        creds = google_oauth.credentials_for(sub)
        from googleapiclient.discovery import build
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        drive.permissions().create(
            fileId=spreadsheet_id,
            body={"type": "user", "role": role, "emailAddress": email},
            sendNotificationEmail=False,
        ).execute()

    def _drive_unshare(sub: str, spreadsheet_id: str, email: str) -> None:
        """Remove a user's Drive permission on a Google Sheet."""
        from .. import google_oauth
        creds = google_oauth.credentials_for(sub)
        from googleapiclient.discovery import build
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        perms = drive.permissions().list(
            fileId=spreadsheet_id, fields="permissions(id,emailAddress)",
        ).execute().get("permissions", [])
        for p in perms:
            if (p.get("emailAddress") or "").lower() == email.lower():
                drive.permissions().delete(fileId=spreadsheet_id, permissionId=p["id"]).execute()
                return

    @mcp.tool()
    async def data_share(namespace: str, email: str, permission: str = "write") -> dict:
        """Share a namespace with another oto user (by email).

        Shares both in the oto DB and in Google Drive (so the recipient
        can access the Sheet with their own Google account).

        Args:
            namespace: Namespace to share (must be owned by you).
            email: Email of the recipient oto user.
            permission: 'read' or 'write' (default write).
        """
        sub = access.current_user_sub_or_raise()
        if permission not in ("read", "write"):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="permission must be 'read' or 'write'"))
        recipient = db.get_user_by_email(email)
        if not recipient:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"aucun utilisateur oto avec l'email {email}"))
        ns = db.get_datastore_namespace(sub, namespace)
        if not ns:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` not found"))
        try:
            db.share_datastore_namespace(sub, namespace, recipient["sub"], permission)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        drive_role = "writer" if permission == "write" else "reader"
        try:
            _drive_share(sub, ns["spreadsheet_id"], email, drive_role)
        except Exception as e:
            return {"ok": True, "namespace": namespace, "shared_with": email, "permission": permission,
                    "drive_warning": f"DB partagé mais Drive share échoué : {e}"}
        return {"ok": True, "namespace": namespace, "shared_with": email, "permission": permission}

    @mcp.tool()
    async def data_unshare(namespace: str, email: str) -> dict:
        """Remove a user's access to a shared namespace (DB + Google Drive).

        Args:
            namespace: Namespace to unshare (must be owned by you).
            email: Email of the user to remove.
        """
        sub = access.current_user_sub_or_raise()
        recipient = db.get_user_by_email(email)
        if not recipient:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"aucun utilisateur oto avec l'email {email}"))
        ns = db.get_datastore_namespace(sub, namespace)
        removed = db.unshare_datastore_namespace(sub, namespace, recipient["sub"])
        if not removed:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"pas de partage actif pour {email} sur {namespace}"))
        if ns:
            try:
                _drive_unshare(sub, ns["spreadsheet_id"], email)
            except Exception:
                pass
        return {"ok": True, "namespace": namespace, "removed": email}
