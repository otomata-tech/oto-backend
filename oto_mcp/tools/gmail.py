"""Gmail — surface oto-core (GmailClient) exposée par-utilisateur, multi-compte.

Chaque user connecte un ou plusieurs comptes Google sur
`https://manage.oto.cx/` (section Google) via le flow OAuth unifié (scope
`gmail.modify`). Les tools `gmail_*` agissent sur le compte par défaut, ou sur
le compte ciblé par le paramètre `account` (l'adresse email).

Pas de clé plateforme : l'accès est strictement per-user via OAuth (comme le
datastore et WhatsApp), donc pas de `resolve_api_key` ici.

Surface regroupée (6 tools) : énumérer les comptes, chercher, lire, lister les
brouillons, **composer** (envoi/brouillon, nouveau ou réponse) et **modifier**
(archiver/corbeille).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_content, file_source, google_oauth


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _client_for_user(account: Optional[str] = None):
    """Instancie un GmailClient oto-core avec les credentials du user.

    `account` (email) cible un compte précis ; None = compte par défaut.
    Lève une McpError actionnable si aucun compte Google n'est connecté.
    """
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.gmail.lib.gmail_client import GmailClient
    return GmailClient(credentials=creds)


def _resolve_attachments(attachments):
    """Résout des refs `file_source` en fichiers TEMPORAIRES (le GmailClient attend
    des CHEMINS locaux pour ses pièces jointes, or le serveur n'a pas le disque de
    l'utilisateur). `attachments` = liste de `{"kind":"drive|gmail|url", …}` (cf.
    file_source.resolve). Renvoie `(paths, cleanup)` — l'appelant DOIT appeler
    `cleanup()` en finally. Lève FileSourceError sur une ref illisible (nettoie
    d'abord le temp déjà écrit)."""
    if not attachments:
        return [], (lambda: None)
    tmpdir = tempfile.mkdtemp(prefix="oto-gmail-att-")

    def cleanup():
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        paths = []
        for i, src in enumerate(attachments):
            rf = file_source.resolve(src)
            # basename défensif : jamais laisser un filename traverser le tmpdir.
            name = os.path.basename(rf.filename or "") or f"attachment-{i}"
            path = os.path.join(tmpdir, name)
            with open(path, "wb") as f:
                f.write(rf.data)
            paths.append(path)
        return paths, cleanup
    except Exception:
        cleanup()
        raise


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def gmail_list_accounts() -> dict:
        """List the Google accounts the user has connected.

        Returns {accounts: [{email, is_default}]}. Use an `email` value as the
        `account` argument of the other gmail_* tools to act on a specific
        account; omit `account` to use the default.
        """
        sub = access.current_user_sub_or_raise()
        accounts = google_oauth.list_accounts(sub)
        return {
            "accounts": [
                {"email": a.get("google_email"), "is_default": a.get("is_default", False)}
                for a in accounts
            ]
        }

    @mcp.tool()
    async def gmail_search(query: str, max_results: int = 20, account: Optional[str] = None) -> dict:
        """Search the user's Gmail with Gmail query syntax.

        Args:
            query: Gmail search query (e.g. `from:foo@bar.com is:unread newer_than:7d`).
            max_results: max messages to return (default 20).
            account: email of the Google account to use (default account if omitted).

        Returns {messages: [{id, threadId, from, subject, date, snippet, labelIds}]}.
        """
        client = _client_for_user(account)
        messages = await asyncio.to_thread(client.search, query, max_results)
        return {"messages": messages, "count": len(messages)}

    @mcp.tool()
    async def gmail_get(message_id: str, account: Optional[str] = None) -> dict:
        """Fetch a full message (headers, body, attachment metadata).

        Args:
            message_id: Gmail message id.
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(client.get_message, message_id)

    @mcp.tool()
    async def gmail_get_attachment(
        message_id: str, filename: str, index: int = 0, account: Optional[str] = None
    ) -> dict:
        """Fetch the CONTENT of a Gmail attachment, by filename.

        Identify the attachment by its `filename` (from the `attachments` list of
        `gmail_get`). The response depends on the file:
        - **small text** (JSON/CSV/Markdown/plain, ≤256 KB) → returned INLINE:
          `{encoding: "text", content: "<decoded text>"}` — read it directly.
        - **binary or large** (PDF, image, big file) → uploaded to temporary
          storage and returned as a short-lived signed URL: `{encoding: "url",
          url, expires_in}` (seconds). Fetch the URL to get the bytes.

        Args:
            message_id: Gmail message id (the one passed to gmail_get).
            filename: name of the attachment to fetch (e.g. "Contrat.pdf").
            index: 0-based tiebreaker if several attachments share that name
                (e.g. inline images); default 0 = the first one.
            account: email of the Google account to use (default if omitted).

        Returns {filename, mimeType, size, encoding, content|url, expires_in?}.
        """
        client = _client_for_user(account)
        try:
            att = await asyncio.to_thread(client.get_attachment, message_id, filename, index)
        except Exception as e:
            raise _bad(str(e))
        data, filename, mime = att["data"], att["filename"], att["mimeType"]
        sub = access.current_user_sub_or_raise()
        try:
            return await asyncio.to_thread(
                file_content.render_for_agent, data, filename, mime,
                sub=sub, prefix="gmail-attachments")
        except file_content.MediaUnavailable as e:
            raise _bad(str(e))

    @mcp.tool()
    async def gmail_list_drafts(max_results: int = 20, account: Optional[str] = None) -> dict:
        """List the user's Gmail drafts.

        Returns {drafts: [{id, message_id, to, subject, date, snippet}]}.
        """
        client = _client_for_user(account)
        drafts = await asyncio.to_thread(client.list_drafts, max_results)
        return {"drafts": drafts, "count": len(drafts)}

    @mcp.tool()
    async def gmail_compose(
        body: str,
        mode: str = "send",
        to: Optional[str] = None,
        subject: Optional[str] = None,
        reply_to: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: Optional[str] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
        account: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
    ) -> dict:
        """Compose an email — send or draft, new message or reply.

        Args:
            body: message body (rendered from markdown to HTML by default).
            mode: "send" (default) to send now, or "draft" to save for human review.
            to: recipient(s), comma-separated. REQUIRED for a new message (omit when replying).
            subject: subject line (new message only; a reply keeps the thread's subject).
            reply_to: id of the message to reply to. When set, this is a threaded REPLY
                (subject/thread preserved) and `to`/`subject` are ignored.
            cc / bcc: optional carbon copy (bcc: new message only).
            html: explicit HTML body (bypasses markdown rendering).
            from_name: optional display name for the From header.
            markdown: render `body` from markdown when `html` is absent (default True).
            account: email of the Google account to use (default if omitted).
            attachments: files to attach, as `source` refs oto resolves server-side
                (the agent has no local disk). Each item — `kind` selects the origin:
                - Drive: `{"kind":"drive","file_id":"<id>"}` (id from drive_list/metadata)
                - Gmail: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
                - URL:   `{"kind":"url","url":"https://…"}` — e.g. a signed URL from
                  `oto_upload_url` (upload a local PDF first) or drive_download.

        Returns the created/sent message ids.
        """
        if mode not in ("send", "draft"):
            raise _bad("mode doit être 'send' ou 'draft'.")
        client = _client_for_user(account)
        try:
            att = _resolve_attachments(attachments)
        except file_source.FileSourceError as e:
            raise _bad(str(e))
        att_paths, _cleanup = att
        try:
            if reply_to:
                if mode == "draft":
                    return await asyncio.to_thread(
                        lambda: client.create_draft_reply(
                            message_id=reply_to, body=body, html=html, cc=cc, markdown=markdown,
                            attachments=att_paths,
                        )
                    )
                return await asyncio.to_thread(
                    lambda: client.reply(
                        message_id=reply_to, body=body, html=html, cc=cc,
                        from_name=from_name, markdown=markdown, attachments=att_paths,
                    )
                )
            if not to:
                raise _bad("`to` requis pour un nouveau message (ou fournis `reply_to` pour répondre).")
            if mode == "draft":
                return await asyncio.to_thread(
                    lambda: client.create_draft(
                        to=to, subject=subject or "", body=body, html=html, cc=cc, bcc=bcc,
                        attachments=att_paths,
                    )
                )
            return await asyncio.to_thread(
                lambda: client.send(
                    to=to, subject=subject or "", body=body, html=html,
                    cc=cc, bcc=bcc, from_name=from_name, markdown=markdown, attachments=att_paths,
                )
            )
        finally:
            _cleanup()

    @mcp.tool()
    async def gmail_modify(
        message_ids: list[str], action: str, account: Optional[str] = None,
    ) -> dict:
        """Archive or trash messages.

        Args:
            message_ids: Gmail message ids to act on.
            action: "archive" (remove the INBOX label) or "trash" (move to trash).
            account: email of the Google account to use (default if omitted).
        """
        if not message_ids:
            raise _bad("message_ids requis")
        if action not in ("archive", "trash"):
            raise _bad("action doit être 'archive' ou 'trash'.")
        client = _client_for_user(account)
        if action == "archive":
            results = await asyncio.to_thread(client.archive_messages, message_ids)
            return {"archived": results}
        trashed = []
        for mid in message_ids:
            res = await asyncio.to_thread(client.trash_message, mid)
            trashed.append(res.get("id", mid))
        return {"trashed": trashed}
