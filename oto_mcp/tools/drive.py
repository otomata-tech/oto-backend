"""Google Drive — surface oto-core (DriveClient) exposée par-utilisateur, multi-compte.

Gestion des fichiers/dossiers du Drive du user : lister, organiser (déplacer,
renommer, dossiers), supprimer, partager. Scope `/auth/drive` **complet**
(restricted) — pour voir/gérer TOUS les fichiers, pas seulement ceux créés par
oto. Compte par défaut ou ciblé par `account`. Per-user via OAuth.

L'**upload** local→Drive reste côté CLI (pas de FS serveur). La LECTURE, elle, est
exposée en entier et sans disque : `op="download"` pour les fichiers binaires/
uploadés, `op="export"` pour les Google natifs (Docs/Sheets/Slides — leur contenu
ne se télécharge pas, il se convertit). Les deux rendent le contenu à l'agent
(inline texte, ou URL signée pour un binaire). L'argument « pas de FS » ne valait
pas pour l'export : convertir en mémoire n'écrit rien (signal #329 — sans lui,
impossible d'ingérer les notes de réunion Gemini autrement qu'au copier-coller).

**Surface consolidée (ADR 0047 §Amendement, appliqué au produit `drive` du
connecteur `google`)** : un tool par OBJET métier, le verbe en paramètre `op` —
`drive_file` (list/metadata/download/export/create_folder/update/delete), tous
scopés par le même fichier/dossier (`file_id`) et le même `account`.
`drive_access` reste SEUL : son vocabulaire (`email`/`role`/`remove`/`notify`)
est celui d'un AUTRE objet — la permission — et ne recouvre aucun paramètre de
`drive_file`. Le fusionner mettrait « changer qui voit ce fichier » dans la même
énumération d'`op` que « supprimer ce fichier » : deux gestes irréversibles à une
faute de frappe l'un de l'autre, pour zéro paramètre factorisé.

⚠️ Ce module ÉCRIT sur les données personnelles du user. `op` vaut `"list"` par
défaut (une LECTURE) : un appel sans `op` ne peut ni supprimer ni modifier. Les
arguments obligatoires d'une op manquants lèvent une erreur nommant l'op et
l'argument — jamais de repli silencieux.
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
    """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback.

    Vaut d'abord pour les ops destructrices : un `file_id` absent doit dire lequel
    manque, pas partir chez Google avec `None` (ni, pire, viser autre chose)."""
    if value is None:
        raise _bad(f"op='{op}' requiert {name}")
    return value


# Formats d'export offerts à l'agent (un mot, pas un mime à recopier).
_EXPORT_MIME = {
    "markdown": "text/markdown",
    "md": "text/markdown",
    "text": "text/plain",
    "txt": "text/plain",
    "html": "text/html",
    "pdf": "application/pdf",
    "csv": "text/csv",
}

# Défaut par type SOURCE : un tableur n'a pas de markdown, une présentation non plus.
# Sert aussi de test « est-ce un natif Google ? » — sinon c'est op="download".
_DEFAULT_EXPORT_BY_SOURCE = {
    "application/vnd.google-apps.document": "text/markdown",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _client_for_user(account: Optional[str] = None):
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="drive")
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.drive.lib.drive_client import DriveClient
    return DriveClient(credentials=creds)


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


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def drive_file(
        op: Literal["list", "metadata", "download", "export", "create_folder",
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
          - **binary or large** (PDF, image, big file) → uploaded to temporary
            storage and returned as a short-lived signed URL: `{encoding: "url",
            url, expires_in}` (seconds). Fetch the URL to get the bytes.
          - **spreadsheet (.xlsx)** → returned INLINE as CSV, one section per
            sheet: `{encoding: "text", format: "csv", content, sheets,
            sheet_names, truncated}`. Each section starts with `# sheet=<index>
            name="…" rows_total=… rows_rendered=… truncated=…` then the CSV rows
            (computed values, not formulas; dates ISO 8601). All sheets by
            default, `max_rows` rows each (default 200, max 5000), size-capped:
            if `truncated`, ask one sheet with `sheet` and/or raise `max_rows`;
            a truncated render also carries `raw_url` (+ `raw_expires_in`, seconds):
            a short-lived signed URL to the FULL original file, e.g. to load it
            into a table or store it.
          For a Google-native doc (Docs/Sheets/Slides), this fails — read those
          with op="export" instead (they are converted, not downloaded). Returns
          {filename, mimeType, size, encoding, content|url, expires_in?}.
        - **"export"**: Read the CONTENT of a GOOGLE-NATIVE doc (Docs/Sheets/
          Slides), by `file_id`. The counterpart of op="download", which only works
          on uploaded/binary files: a Google-native doc has no binary content to
          download (403 "Only files with binary content can be downloaded"), its
          content comes out of an EXPORT — that's this op. Use it to actually READ
          meeting notes ("… - Notes par Gemini"), specs, or any Doc you found with
          op="list". `format` = markdown | text | html | pdf | csv ; omit it for a
          sensible default per type (Doc→markdown, Sheet→csv (first sheet),
          Slides→text). Returns {filename, mimeType, encoding, content|url, …}:
          text formats come back INLINE, `pdf` as a short-lived signed URL.
        - **"create_folder"**: Create a folder (`name`), optionally inside a parent
          folder (`parent_folder_id`). Returns the folder metadata. Folders only —
          uploading a local FILE to Drive stays on the CLI side (no server FS).
        - **"update"**: Rename and/or move a file/folder. `new_name` = new name
          (rename) ; `move_to_folder` = destination folder id (move). You can do
          both at once ; at least one of the two is required.
        - **"delete"**: ⚠️ DESTRUCTIVE — delete a file/folder (moves it to trash).
          Irreversible from the API's point of view.

        Sharing is NOT here: who can access a file is `drive_access` (list, grant
        or revoke), a separate tool on purpose.

        Args:
            op: list (default) | metadata | download | export | create_folder |
                update | delete. The default is a READ — an omitted `op` never
                writes and never deletes.
            file_id: op="metadata"/"download"/"export"/"update"/"delete" — the
                file (or folder) id, from op="list".
            folder_id: op="list" — restrict to a parent folder id.
            query: op="list" — raw Drive query (e.g. "name contains 'report'",
                "mimeType='application/pdf'").
            page_size: op="list" — max results (paginates).
            format: op="export" — markdown | text | html | pdf | csv. Omit for a
                sensible default per type (Doc→markdown, Sheet→csv (first sheet),
                Slides→text).
            name: op="create_folder" — the folder name.
            parent_folder_id: op="create_folder" — create it inside this folder.
            new_name: op="update" — new name (rename).
            move_to_folder: op="update" — destination folder id (move).
            sheet: op="download" of an .xlsx — the sheet to read, by name or
                0-based index (see `sheet_names`). Omit for all sheets.
            max_rows: op="download" of an .xlsx — rows per sheet (default 200,
                max 5000).
            account: email of the Google account to use (default if omitted).
        """
        if (sheet is not None or max_rows is not None) and op != "download":
            raise _bad(f"`sheet`/`max_rows` ne valent que pour op='download' d'un "
                       f"tableur .xlsx (reçu op='{op}').")
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
                raise _bad(f"format « {format} » inconnu — attendu : "
                           f"{', '.join(sorted(_EXPORT_MIME))}.")
            if mime is None:
                meta = await asyncio.to_thread(client.get_file_metadata, fid)
                src = (meta.get("mimeType") or "")
                mime = _DEFAULT_EXPORT_BY_SOURCE.get(src)
                if mime is None:
                    # Pas un natif Google : l'export ne s'applique pas, le download si.
                    raise _bad(f"« {meta.get('name') or fid} » n'est pas un document "
                               f"Google natif (mimeType {src or 'inconnu'}) : son contenu "
                               f"se lit avec op='download', pas op='export'.")
            try:
                f = await asyncio.to_thread(client.export_file_bytes, fid, mime)
            except Exception as e:
                raise _bad(str(e))
            sub = access.current_user_sub_or_raise()
            try:
                return await asyncio.to_thread(
                    file_content.render_for_agent, f["data"], f["filename"], mime,
                    sub=sub, prefix="drive-exports")
            except file_content.MediaUnavailable as e:
                raise _bad(str(e))

        if op == "create_folder":
            return await asyncio.to_thread(client.create_folder,
                                           _need(name, "name", op),
                                           parent_folder_id)

        if op == "update":
            fid = _need(file_id, "file_id", op)
            if not new_name and not move_to_folder:
                raise _bad("op='update' requiert `new_name` (renommer) et/ou "
                           "`move_to_folder` (déplacer).")
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

        raise _bad("op doit être 'list', 'metadata', 'download', 'export', "
                   "'create_folder', 'update' ou 'delete'")

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
            account: email of the Google account to use (default if omitted).
        """
        client = await _client_for_user_async(account)
        if not email:
            perms = await asyncio.to_thread(client.list_permissions, file_id)
            return {"permissions": perms, "count": len(perms)}
        if remove:
            return await asyncio.to_thread(client.unshare, file_id, email)
        return await asyncio.to_thread(client.share, file_id, email, role, notify)
