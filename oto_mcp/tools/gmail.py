"""Gmail — surface oto-core (GmailClient) exposée par-utilisateur, multi-compte.

Chaque user connecte un ou plusieurs comptes Google sur
`https://app.oto.ninja/` (section Google) via le flow OAuth unifié (scope
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
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, google_oauth


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

        Returns the created/sent message ids.
        """
        if mode not in ("send", "draft"):
            raise _bad("mode doit être 'send' ou 'draft'.")
        client = _client_for_user(account)
        if reply_to:
            if mode == "draft":
                return await asyncio.to_thread(
                    lambda: client.create_draft_reply(
                        message_id=reply_to, body=body, html=html, cc=cc, markdown=markdown,
                    )
                )
            return await asyncio.to_thread(
                lambda: client.reply(
                    message_id=reply_to, body=body, html=html, cc=cc,
                    from_name=from_name, markdown=markdown,
                )
            )
        if not to:
            raise _bad("`to` requis pour un nouveau message (ou fournis `reply_to` pour répondre).")
        if mode == "draft":
            return await asyncio.to_thread(
                lambda: client.create_draft(
                    to=to, subject=subject or "", body=body, html=html, cc=cc, bcc=bcc,
                )
            )
        return await asyncio.to_thread(
            lambda: client.send(
                to=to, subject=subject or "", body=body, html=html,
                cc=cc, bcc=bcc, from_name=from_name, markdown=markdown,
            )
        )

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
