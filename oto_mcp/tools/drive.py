"""Google Drive — surface oto-core (DriveClient) exposée par-utilisateur, multi-compte.

Gestion des fichiers/dossiers du Drive du user : lister, organiser (déplacer,
renommer, dossiers), supprimer, partager. Scope `/auth/drive` **complet**
(restricted) — pour voir/gérer TOUS les fichiers, pas seulement ceux créés par
oto. Compte par défaut ou ciblé par `account`. Per-user via OAuth.

L'**upload** local→Drive et l'export Google natif restent côté CLI (pas de FS
serveur). En revanche le **download bytes** EST exposé (`drive_download`) : il rend
le contenu à l'agent (inline texte, ou URL signée pour un binaire) sans disque.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_content, google_oauth


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.drive.lib.drive_client import DriveClient
    return DriveClient(credentials=creds)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def drive_list(
        folder_id: Optional[str] = None,
        query: Optional[str] = None,
        page_size: int = 100,
        account: Optional[str] = None,
    ) -> dict:
        """List Drive files.

        Args:
            folder_id: restrict to a parent folder id.
            query: raw Drive query (e.g. "name contains 'report'", "mimeType='application/pdf'").
            page_size: max results (paginates).
            account: email of the Google account to use (default if omitted).

        Returns {files: [{id, name, mimeType, modifiedTime, size, webViewLink}], count}.
        """
        client = _client_for_user(account)
        files = await asyncio.to_thread(client.list_files, folder_id, query, page_size)
        return {"files": files, "count": len(files)}

    @mcp.tool()
    async def drive_download(file_id: str, account: Optional[str] = None) -> dict:
        """Fetch the CONTENT (bytes) of a Drive file, by file_id.

        Get `file_id` from `drive_list`/`drive_metadata`. The response depends on
        the file:
        - **small text** (txt/csv/json/markdown, ≤256 KB) → returned INLINE:
          `{encoding: "text", content}` — read it directly.
        - **binary or large** (PDF, image, big file) → uploaded to temporary
          storage and returned as a short-lived signed URL: `{encoding: "url",
          url, expires_in}` (seconds). Fetch the URL to get the bytes.

        For a Google-native doc (Docs/Sheets/Slides), this fails — those must be
        exported (CLI), not downloaded. Returns {filename, mimeType, size,
        encoding, content|url, expires_in?}.
        """
        client = _client_for_user(account)
        try:
            f = await asyncio.to_thread(client.get_file_bytes, file_id)
        except Exception as e:
            raise _bad(str(e))
        data, filename, mime = f["data"], f["filename"], f["mimeType"]
        out = {"filename": filename, "mimeType": mime, "size": len(data)}
        text = file_content.as_text(data, mime)
        if text is not None and len(data) <= file_content.INLINE_TEXT_CAP:
            out.update(encoding="text", content=text)
            return out
        from .. import media_store
        sub = access.current_user_sub_or_raise()
        try:
            url = await asyncio.to_thread(
                media_store.upload_private, "drive-files", sub, data, mime, filename)
        except media_store.MediaError as e:
            raise _bad(
                f"Fichier binaire/volumineux ({len(data)} octets) : stockage "
                f"temporaire indisponible pour produire une URL ({e}).")
        out.update(encoding="url", url=url, expires_in=media_store.presign_expiry())
        return out

    @mcp.tool()
    async def drive_metadata(file_id: str, account: Optional[str] = None) -> dict:
        """Get a Drive file's metadata by id."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.get_file_metadata, file_id)

    @mcp.tool()
    async def drive_create_folder(
        name: str, parent_folder_id: Optional[str] = None, account: Optional[str] = None,
    ) -> dict:
        """Create a folder, optionally inside a parent folder. Returns the folder metadata."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.create_folder, name, parent_folder_id)

    @mcp.tool()
    async def drive_update(
        file_id: str,
        new_name: Optional[str] = None,
        move_to_folder: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Rename and/or move a file/folder.

        Args:
            new_name: new name (rename).
            move_to_folder: destination folder id (move). You can do both at once.
            account: email of the Google account to use (default if omitted).
        """
        if not new_name and not move_to_folder:
            raise _bad("Fournis `new_name` (renommer) et/ou `move_to_folder` (déplacer).")
        client = _client_for_user(account)
        out: dict = {}
        if new_name:
            out["renamed"] = await asyncio.to_thread(client.rename_file, file_id, new_name)
        if move_to_folder:
            out["moved"] = await asyncio.to_thread(client.move_file, file_id, move_to_folder)
        return out

    @mcp.tool()
    async def drive_delete(file_id: str, account: Optional[str] = None) -> dict:
        """Delete a file/folder (moves it to trash). Irreversible from the API's point of view."""
        client = _client_for_user(account)
        return await asyncio.to_thread(client.delete_file, file_id)

    @mcp.tool()
    async def drive_access(
        file_id: str,
        email: Optional[str] = None,
        role: str = "reader",
        remove: bool = False,
        notify: bool = True,
        account: Optional[str] = None,
    ) -> dict:
        """Inspect or change who can access a file/folder.

        Args:
            email: the person to share with / revoke. OMIT to just LIST current access.
            role: "reader", "commenter" or "writer" (when granting).
            remove: True + `email` → revoke that person's access.
            notify: send Google's notification email (when granting).
            account: email of the Google account to use (default if omitted).

        No `email` → {permissions: [...], count}. With `email` → grants (or
        revokes if `remove`) and returns the operation result.
        """
        client = _client_for_user(account)
        if not email:
            perms = await asyncio.to_thread(client.list_permissions, file_id)
            return {"permissions": perms, "count": len(perms)}
        if remove:
            return await asyncio.to_thread(client.unshare, file_id, email)
        return await asyncio.to_thread(client.share, file_id, email, role, notify)
