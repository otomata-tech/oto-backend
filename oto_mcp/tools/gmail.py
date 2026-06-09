"""Gmail — surface oto-cli (GmailClient) exposée par-utilisateur, multi-compte.

Chaque user connecte un ou plusieurs comptes Google sur
`https://app.oto.ninja/` (section Google) via le flow OAuth unifié
(Sheets+Drive+Gmail, scope `gmail.modify`). Les tools `gmail_*` agissent sur
le compte par défaut, ou sur le compte ciblé par le paramètre `account`
(l'adresse email). `gmail_list_accounts` énumère les comptes connectés.

Pas de clé plateforme : l'accès est strictement per-user via OAuth (comme
le datastore et WhatsApp), donc pas de `resolve_api_key` ici.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, google_oauth


def _client_for_user(account: Optional[str] = None):
    """Instancie un GmailClient oto-cli avec les credentials du user.

    `account` (email) cible un compte précis ; None = compte par défaut.
    Lève une McpError actionnable si aucun compte Google n'est connecté.
    """
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    from oto.tools.google.gmail.lib.gmail_client import GmailClient
    return GmailClient(credentials=creds)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def gmail_list_accounts() -> dict:
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
    async def gmail_send(
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: Optional[str] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
        account: Optional[str] = None,
    ) -> dict:
        """Send an email from the user's Gmail.

        Args:
            to: recipient(s), comma-separated.
            subject: subject line.
            body: message body. Rendered from markdown to HTML by default
                (set markdown=False or pass `html` to override).
            cc / bcc: optional carbon copy recipients.
            html: explicit HTML body (bypasses markdown rendering).
            from_name: optional display name for the From header.
            markdown: render `body` from markdown when `html` is not given (default True).
            account: email of the Google account to send from (default if omitted).

        Returns {id, threadId}.
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(
            lambda: client.send(
                to=to, subject=subject, body=body, html=html,
                cc=cc, bcc=bcc, from_name=from_name, markdown=markdown,
            )
        )

    @mcp.tool()
    async def gmail_reply(
        message_id: str,
        body: str,
        cc: Optional[str] = None,
        html: Optional[str] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
        draft: bool = False,
        account: Optional[str] = None,
    ) -> dict:
        """Reply to a message, preserving thread, subject, and headers.

        With draft=True, creates a threaded reply DRAFT instead of sending —
        for replies that need human review in Gmail before going out.

        Args:
            message_id: id of the message to reply to.
            body: reply body (markdown-rendered by default).
            cc: optional carbon copy.
            html: explicit HTML body (bypasses markdown rendering).
            from_name: optional display name for the From header (ignored in draft mode).
            markdown: render `body` from markdown when `html` is not given (default True).
            draft: save as draft in the thread instead of sending (default False).
            account: email of the Google account to use (default if omitted).

        Returns {id, threadId, to} when sent ; {id, message_id, threadId} when draft=True.
        """
        client = _client_for_user(account)
        if draft:
            return await asyncio.to_thread(
                lambda: client.create_draft_reply(
                    message_id=message_id, body=body, html=html,
                    cc=cc, markdown=markdown,
                )
            )
        return await asyncio.to_thread(
            lambda: client.reply(
                message_id=message_id, body=body, html=html,
                cc=cc, from_name=from_name, markdown=markdown,
            )
        )

    @mcp.tool()
    async def gmail_create_draft(
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Create a NEW draft email (not sent, new thread).

        For a threaded REPLY draft (attached to an existing conversation), use
        gmail_reply with draft=True instead.

        Args:
            to: recipient(s).
            subject: subject line.
            body: message body.
            cc / bcc: optional carbon copy recipients.
            html: optional explicit HTML body.
            account: email of the Google account to use (default if omitted).

        Returns {id, message_id, threadId}.
        """
        client = _client_for_user(account)
        return await asyncio.to_thread(
            lambda: client.create_draft(
                to=to, subject=subject, body=body, html=html, cc=cc, bcc=bcc,
            )
        )

    @mcp.tool()
    async def gmail_list_drafts(max_results: int = 20, account: Optional[str] = None) -> dict:
        """List the user's Gmail drafts.

        Args:
            max_results: max drafts to return (default 20).
            account: email of the Google account to use (default if omitted).

        Returns {drafts: [{id, message_id, to, subject, date, snippet}]}.
        """
        client = _client_for_user(account)
        drafts = await asyncio.to_thread(client.list_drafts, max_results)
        return {"drafts": drafts, "count": len(drafts)}

    @mcp.tool()
    async def gmail_archive(message_ids: list[str], account: Optional[str] = None) -> dict:
        """Archive messages (remove the INBOX label).

        Args:
            message_ids: list of Gmail message ids to archive.
            account: email of the Google account to use (default if omitted).

        Returns {archived: [{id, labelIds}]}.
        """
        if not message_ids:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="message_ids requis"))
        client = _client_for_user(account)
        results = await asyncio.to_thread(client.archive_messages, message_ids)
        return {"archived": results}

    @mcp.tool()
    async def gmail_trash(message_id: str, account: Optional[str] = None) -> dict:
        """Move a message to trash.

        Args:
            message_id: Gmail message id.
            account: email of the Google account to use (default if omitted).
        """
        client = _client_for_user(account)
        res = await asyncio.to_thread(client.trash_message, message_id)
        return {"ok": True, "id": res.get("id", message_id)}
