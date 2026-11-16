"""Google Drive — oto-core surface (DriveClient) exposed per-user, multi-account.

Management of the user's Drive files/folders: list, organize (move,
rename, folders), delete, share. **Full** `/auth/drive` scope
(restricted) — to see/manage ALL files, not only those created by
oto. Default account or targeted by `account`. Per-user via OAuth.

A LOCAL file's upload stays on the CLI side (no server FS) ; `op="upload"` deposits a
file REFERENCE oto resolves itself (`source`: http, url, project_file, drive…), and
can convert it to its Google-native type (an .xlsx becomes a Google Sheet, formatting
kept). READING is
fully exposed and diskless: `op="download"` for binary/uploaded files,
`op="export"` for Google-native ones (Docs/Sheets/Slides — their content
cannot be downloaded, it is converted). Both return the content to the agent
(inline text, or a signed URL for a binary). The "no FS" argument did not hold
for export: converting in memory writes nothing (signal #329 — without it,
impossible to ingest Gemini meeting notes other than by copy-paste).

**Consolidated surface (ADR 0047 §Amendment, applied to the `drive` product of the
`google` connector)**: one tool per business OBJECT, the verb as an `op` parameter —
`drive_file` (list/metadata/download/export/upload/create_folder/update/delete), all
scoped by the same file/folder (`file_id`) and the same `account`.
`drive_access` stays ALONE: its vocabulary (`email`/`role`/`remove`/`notify`)
is that of ANOTHER object — the permission — and overlaps no parameter of
`drive_file`. Merging it would put "change who sees this file" in the same
`op` enumeration as "delete this file": two irreversible gestures one
typo apart, for zero shared parameters.

⚠️ This module WRITES to the user's personal data. `op` defaults to `"list"`
(a READ): a call without `op` can neither delete nor modify. Missing required
arguments of an op raise an error naming the op and
the argument — never a silent fallback.
"""
from __future__ import annotations

import asyncio
from typing import Literal, Optional, Union

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_content
from ..auth import google as google_oauth


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Required argument for THIS op — actionable error, never a fallback.

    Applies first to destructive ops: a missing `file_id` must say which one
    is missing, not go off to Google with `None` (or, worse, target something else)."""
    if value is None:
        raise _bad(f"op='{op}' requires {name}")
    return value


_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Office exports are FILES to pass on (a workbook to send, a deck or a document to
# build on), never content to read: served as a signed URL, with their extension.
_OFFICE_EXT = {_XLSX: ".xlsx", _PPTX: ".pptx", _DOCX: ".docx"}

# Export formats offered to the agent (a single word, not a mime to copy out).
_EXPORT_MIME = {
    "markdown": "text/markdown",
    "md": "text/markdown",
    "text": "text/plain",
    "txt": "text/plain",
    "html": "text/html",
    "pdf": "application/pdf",
    "csv": "text/csv",
    # A Sheets workbook with ALL its tabs (csv exports the first one only). Binary:
    # served as a signed URL, never rendered inline (signal #1376, 10/10/2026).
    "xlsx": _XLSX,
    # A Slides deck / a Doc as an editable Office file, e.g. a customer's template an
    # agent builds new decks from, then uploads back with op="upload" convert=true.
    "pptx": _PPTX,
    "docx": _DOCX,
}

# Default per SOURCE type: a spreadsheet has no markdown, nor does a presentation.
# Also serves as the "is it Google-native?" test — otherwise it is op="download".
_DEFAULT_EXPORT_BY_SOURCE = {
    "application/vnd.google-apps.document": "text/markdown",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _office_file_url(data: bytes, filename: str, sub: str, mime: str) -> dict:
    """An exported Office file, deposited privately and served as a signed URL."""
    from .. import media_store
    ext = _OFFICE_EXT[mime]
    name = filename if filename.lower().endswith(ext) else f"{filename}{ext}"
    try:
        url = media_store.upload_private("drive-exports", sub, data, mime, name)
    except media_store.MediaError as e:
        raise _bad(f"{ext[1:]} export: temporary storage unavailable ({e}).") from None
    return {"filename": name, "mimeType": mime, "size": len(data),
            "encoding": "url", "url": url, "expires_in": media_store.presign_expiry()}


def _resolve_upload(source: dict):
    """The bytes of an `op="upload"` source, resolved server-side (off the loop)."""
    from .. import file_source
    try:
        return file_source.resolve(source)
    except file_source.FileSourceError as e:
        raise _bad(f"source: {e}") from None


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="drive")
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.drive.lib.drive_client import DriveClient
    return DriveClient(credentials=creds)


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 lot 2 — see gmail.py::_client_for_user_async for the
# rationale (same token-refresh mechanism, same method).
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google did not respond within {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(token refresh) — try again.")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def drive_file(
        op: Literal["list", "metadata", "download", "export", "upload", "create_folder",
                    "update", "delete"] = "list",
        file_id: Optional[str] = None,
        folder_id: Optional[str] = None,
        query: Optional[str] = None,
        page_size: int = 100,
        format: Optional[str] = None,
        name: Optional[str] = None,
        parent_folder_id: Optional[str] = None,
        new_name: Optional[str] = None,
        move_to_folder: Optional[str] = None,
        sheet: Optional[Union[int, str]] = None,
        max_rows: Optional[int] = None,
        source: Optional[dict] = None,
        convert: bool = False,
        account: Optional[str] = None,
    ) -> dict:
        """A file (or folder) in the user's Drive — list, read, organise, delete.

        `op`:
        - **"list"** (default): List Drive files. `folder_id` restricts to a parent
          folder id ; `query` = raw Drive query (e.g. "name contains 'report'",
          "mimeType='application/pdf'") ; `page_size` = max results (paginates).
          Returns {files: [{id, name, mimeType, modifiedTime, size, webViewLink}],
          count}.
        - **"metadata"**: Get a Drive file's metadata by id (`file_id`).
        - **"download"**: Fetch the CONTENT (bytes) of a Drive file, by `file_id`.
          Get `file_id` from op="list" / op="metadata". The response depends on
          the file:
          - **small text** (txt/csv/json/markdown, ≤256 KB) → returned INLINE:
            `{encoding: "text", content}` — read it directly.
          - **PDF** → its extracted TEXT returned INLINE: `{encoding: "text",
            format: "pdf-text", content, pages, truncated}` plus `raw_url` (+
            `raw_expires_in`), a short-lived signed URL to the original PDF
            (layout, images). A scanned or protected PDF has no text: it comes
            back as a URL, with `text_unavailable` saying why.
          - **binary or large** (image, archive, big file) → uploaded to temporary
            storage and returned as a short-lived signed URL: `{encoding: "url",
            url, expires_in}` (seconds). Fetch the URL to get the bytes.
          - **spreadsheet (.xlsx)** → returned INLINE as CSV, one section per
            sheet: `{encoding: "text", format: "csv", content, sheets,
            sheet_names, truncated}`. Each section starts with `# sheet=<index>
            name="…" rows_total=… rows_rendered=… truncated=…` then the CSV rows
            (computed values, not formulas; dates ISO 8601). All sheets by
            default, `max_rows` rows each (default 200, max 5000), size-capped:
            if `truncated`, ask one sheet with `sheet` and/or raise `max_rows`;
            the render always carries `raw_url` (+ `raw_expires_in`, seconds):
            a short-lived signed URL to the FULL original file, e.g. to load it
            into a table, store it or edit it with its formatting.
          For a Google-native doc (Docs/Sheets/Slides), this fails — read those
          with op="export" instead (they are converted, not downloaded). Returns
          {filename, mimeType, size, encoding, content|url, expires_in?}.
        - **"export"**: Read the CONTENT of a GOOGLE-NATIVE doc (Docs/Sheets/
          Slides), by `file_id`. The counterpart of op="download", which only works
          on uploaded/binary files: a Google-native doc has no binary content to
          download (403 "Only files with binary content can be downloaded"), its
          content comes out of an EXPORT — that's this op. Use it to actually READ
          meeting notes ("… - Notes par Gemini"), specs, or any Doc you found with
          op="list". `format` = markdown | text | html | pdf | csv | xlsx | pptx |
          docx ; omit it for a sensible default per type (Doc→markdown, Sheet→csv
          (first sheet), Slides→text). Returns {filename, mimeType, encoding,
          content|url, …}: text formats come back INLINE, `pdf` as its extracted
          text + `raw_url`. `xlsx` (a Sheet, every tab), `pptx` (a Slides deck) and
          `docx` (a Doc) are FILES, served as a signed URL: e.g. take a customer's
          deck as a `.pptx` template, build a new deck from it, and put that back
          with op="upload" convert=true.
        - **"upload"**: Deposit a file oto fetches itself from `source` (e.g.
          `{"kind": "api", "path": "/export.xlsx"}` through your `api` connector,
          `{"kind": "url", "url": …}`, `{"kind": "project_file", …}`) — the bytes
          never go through you. `name` = its name in Drive (the source's by default),
          `parent_folder_id` = its folder. `convert=true` turns it into its
          Google-native type, formatting kept: .xlsx/.xls/.ods/.csv → Google Sheets,
          .docx/.odt/.rtf/.txt/.html → Google Docs, .pptx/.odp → Google Slides ;
          another type is refused. Returns {id, name, mimeType, webViewLink}.
        - **"create_folder"**: Create a folder (`name`), optionally inside a parent
          folder (`parent_folder_id`). Returns the folder metadata.
        - **"update"**: Rename and/or move a file/folder. `new_name` = new name
          (rename) ; `move_to_folder` = destination folder id (move). You can do
          both at once ; at least one of the two is required.
        - **"delete"**: ⚠️ DESTRUCTIVE — delete a file/folder (moves it to trash).
          Irreversible from the API's point of view.

        Sharing is NOT here: who can access a file is `drive_access` (list, grant
        or revoke), a separate tool on purpose.

        Args:
            op: list (default) | metadata | download | export | upload |
                create_folder | update | delete. The default is a READ — an omitted `op` never
                writes and never deletes.
            file_id: op="metadata"/"download"/"export"/"update"/"delete" — the
                file (or folder) id, from op="list".
            folder_id: op="list" — restrict to a parent folder id.
            query: op="list" — raw Drive query (e.g. "name contains 'report'",
                "mimeType='application/pdf'").
            page_size: op="list" — max results (paginates).
            format: op="export" — markdown | text | html | pdf | csv | xlsx | pptx |
                docx. Omit for a sensible default per type (Doc→markdown, Sheet→csv (first sheet),
                Slides→text).
            name: op="create_folder" — the folder name ; op="upload" — the file's
                name in Drive (the source's if omitted).
            parent_folder_id: op="create_folder"/"upload" — create it inside this
                folder.
            new_name: op="update" — new name (rename).
            move_to_folder: op="update" — destination folder id (move).
            sheet: op="download" of an .xlsx — the sheet to read, by name or
                0-based index (see `sheet_names`). Omit for all sheets.
            max_rows: op="download" of an .xlsx — rows per sheet (default 200,
                max 5000).
            source: op="upload" — the file reference oto fetches: `{"kind": "api",
                "path": "/…", "params": {…}}`, `{"kind": "url", "url": …}`,
                `{"kind": "project_file", "project_id": …, "file_id": …}`,
                `{"kind": "drive", "file_id": …}`. ≤ 25 MB.
            convert: op="upload" — true converts to the Google-native type.
            account: email of the Google account to use — same choice as `_account`.
        """
        if (sheet is not None or max_rows is not None) and op != "download":
            raise _bad(f"`sheet`/`max_rows` only apply to op='download' of an "
                       f".xlsx spreadsheet (received op='{op}').")
        if (source is not None or convert) and op != "upload":
            raise _bad(f"`source`/`convert` only apply to op='upload' (received op='{op}').")
        client = await _client_for_user_async(account)

        if op == "list":
            files = await asyncio.to_thread(client.list_files, folder_id, query,
                                            page_size)
            return {"files": files, "count": len(files)}

        if op == "metadata":
            return await asyncio.to_thread(client.get_file_metadata,
                                           _need(file_id, "file_id", op))

        if op == "download":
            fid = _need(file_id, "file_id", op)
            try:
                f = await asyncio.to_thread(client.get_file_bytes, fid)
            except Exception as e:
                raise _bad(str(e))
            data, filename, mime = f["data"], f["filename"], f["mimeType"]
            sub = access.current_user_sub_or_raise()
            try:
                return await asyncio.to_thread(
                    file_content.render_for_agent, data, filename, mime,
                    sub=sub, prefix="drive-files", sheet=sheet, max_rows=max_rows)
            except (file_content.MediaUnavailable, file_content.SpreadsheetError) as e:
                raise _bad(str(e))

        if op == "export":
            fid = _need(file_id, "file_id", op)
            mime = _EXPORT_MIME.get((format or "").strip().lower()) if format else None
            if format and not mime:
                raise _bad(f"unknown format \"{format}\" — expected: "
                           f"{', '.join(sorted(_EXPORT_MIME))}.")
            if mime is None:
                meta = await asyncio.to_thread(client.get_file_metadata, fid)
                src = (meta.get("mimeType") or "")
                mime = _DEFAULT_EXPORT_BY_SOURCE.get(src)
                if mime is None:
                    # Not Google-native: export does not apply, download does.
                    raise _bad(f"\"{meta.get('name') or fid}\" is not a native Google "
                               f"document (mimeType {src or 'unknown'}): its content "
                               f"is read with op='download', not op='export'.")
            try:
                f = await asyncio.to_thread(client.export_file_bytes, fid, mime)
            except Exception as e:
                raise _bad(str(e))
            sub = access.current_user_sub_or_raise()
            if mime in _OFFICE_EXT:
                # The agent asked for a FILE to pass on (a workbook to send, a deck to
                # build on), not for its content: the generic renderer would read an
                # .xlsx back as CSV. Signed URL, with the extension the receiver expects.
                return await asyncio.to_thread(_office_file_url, f["data"], f["filename"],
                                               sub, mime)
            try:
                return await asyncio.to_thread(
                    file_content.render_for_agent, f["data"], f["filename"], mime,
                    sub=sub, prefix="drive-exports")
            except file_content.MediaUnavailable as e:
                raise _bad(str(e))

        if op == "upload":
            rf = await asyncio.to_thread(_resolve_upload, _need(source, "source", op))
            from oto.tools.google.drive.lib.drive_client import DriveClientError
            try:
                return await asyncio.to_thread(
                    client.upload_bytes, rf.data, name or rf.filename, rf.mime,
                    parent_folder_id, convert)
            except DriveClientError as e:
                raise _bad(str(e)) from None

        if op == "create_folder":
            return await asyncio.to_thread(client.create_folder,
                                           _need(name, "name", op),
                                           parent_folder_id)

        if op == "update":
            fid = _need(file_id, "file_id", op)
            if not new_name and not move_to_folder:
                raise _bad("op='update' requires `new_name` (rename) and/or "
                           "`move_to_folder` (move).")
            out: dict = {}
            if new_name:
                out["renamed"] = await asyncio.to_thread(client.rename_file, fid,
                                                         new_name)
            if move_to_folder:
                out["moved"] = await asyncio.to_thread(client.move_file, fid,
                                                       move_to_folder)
            return out

        if op == "delete":
            return await asyncio.to_thread(client.delete_file,
                                           _need(file_id, "file_id", op))

        raise _bad("op must be 'list', 'metadata', 'download', 'export', 'upload', "
                   "'create_folder', 'update' or 'delete'")

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

        No `email` → {permissions: [...], count}. With `email` → grants (or
        revokes if `remove`) and returns the operation result.

        Args:
            email: the person to share with / revoke. OMIT to just LIST current access.
            role: "reader", "commenter" or "writer" (when granting).
            remove: True + `email` → revoke that person's access.
            notify: send Google's notification email (when granting).
            account: email of the Google account to use — same choice as `_account`.
        """
        client = await _client_for_user_async(account)
        if not email:
            perms = await asyncio.to_thread(client.list_permissions, file_id)
            return {"permissions": perms, "count": len(perms)}
        if remove:
            return await asyncio.to_thread(client.unshare, file_id, email)
        return await asyncio.to_thread(client.share, file_id, email, role, notify)
