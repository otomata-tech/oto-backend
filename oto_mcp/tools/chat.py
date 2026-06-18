"""Google Chat — surface oto-core (ChatClient) exposée par-utilisateur, multi-compte.

Lister les espaces (rooms + DM), lire les messages, poster (dans un espace ou en
DM à un user). Scopes **restricted** `chat.spaces.readonly` + `chat.messages`.
Compte par défaut ou ciblé par `account`. Per-user via OAuth.
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
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.chat.lib.chat_client import ChatClient
    return ChatClient(credentials=creds)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def chat_spaces(
        space_type: Optional[str] = None, max_results: int = 100, account: Optional[str] = None,
    ) -> dict:
        """List the Google Chat spaces (rooms + DMs) the user belongs to.

        Args:
            space_type: optional filter — "SPACE" (rooms) or "DIRECT_MESSAGE" (DMs).
            max_results: cap on spaces returned.
            account: email of the Google account to use (default if omitted).

        Returns {spaces: [{name, type, displayName, ...}], count}. Use a `name`
        ('spaces/XXXX') as the `space` argument of the other chat_* tools.
        """
        client = _client_for_user(account)
        filter_ = f'spaceType = "{space_type}"' if space_type else None
        spaces = await asyncio.to_thread(client.list_spaces, filter_, max_results)
        return {"spaces": spaces, "count": len(spaces)}

    @mcp.tool()
    async def chat_messages(space: str, max_results: int = 20, account: Optional[str] = None) -> dict:
        """List recent messages in a space (most recent first). `space` = 'spaces/XXXX'."""
        client = _client_for_user(account)
        messages = await asyncio.to_thread(client.list_messages, space, max_results)
        return {"messages": messages, "count": len(messages)}

    @mcp.tool()
    async def chat_send(
        text: str,
        space: Optional[str] = None,
        user: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Post a Google Chat message — either into a space or as a DM to a user.

        Args:
            text: message text (basic formatting: *bold*, _italic_).
            space: target space resource name ('spaces/XXXX') — for room/space messages.
            user: recipient email — sends a direct message (resolves the DM space).
                Provide EITHER `space` OR `user`, not both.
            account: email of the Google account to use (default if omitted).
        """
        if bool(space) == bool(user):
            raise _bad("Fournis soit `space` (message dans un espace) soit `user` (DM), pas les deux ni aucun.")
        client = _client_for_user(account)
        if user:
            return await asyncio.to_thread(client.send_dm, user, text)
        return await asyncio.to_thread(client.send, space, text)
