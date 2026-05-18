"""Slack — outbound messaging via user token + automation via bot token.

Single-tenant for now: reads `SLACK_BOT_TOKEN` and `SLACK_USER_TOKEN` from the
server's secrets. Multi-tenant migration tracked in #4 (per-user tokens in
the `users` table, OAuth install flow, token rotation).

Default mode for `slack_post_message` is **as_user=True** — outbound human-style
com sent on behalf of the installed user (signature "Oto pour Alexis"). Switch
to `as_user=False` for bot-style automation (notifications, reactions).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.slack import SlackClient

    client = SlackClient(default_as_user=True)

    @mcp.tool()
    async def slack_post_message(
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
        as_user: bool = True,
    ) -> dict:
        """Send a Slack message to a channel or DM.

        Args:
            channel: Channel ID (e.g. C0123456789), DM channel ID (D…), or an
                already-opened conversation. To DM a user by email, call
                `slack_find_user_by_email` + `slack_open_dm` first to get the channel ID.
            text: Message text (Slack mrkdwn supported).
            thread_ts: Parent message ts to reply into a thread.
            as_user: True (default) → message appears as the human user who installed
                the app. False → appears as the bot app. Prefer True for outbound
                human-style com, False for automated notifications.
        """
        return client.post_message(channel, text=text, thread_ts=thread_ts, as_user=as_user)

    @mcp.tool()
    async def slack_delete_message(
        channel: str,
        ts: str,
        as_user: bool = True,
    ) -> dict:
        """Delete a previously posted message.

        Args:
            channel: Channel ID.
            ts: Message timestamp returned by `slack_post_message`.
            as_user: Must match the token used to post (True for user-posted,
                False for bot-posted).
        """
        return client.delete_message(channel, ts, as_user=as_user)

    @mcp.tool()
    async def slack_list_channels(types: str = "public_channel") -> dict:
        """List Slack channels visible to the app.

        Args:
            types: Comma-separated channel types — public_channel, private_channel, mpim, im.
        """
        return {"channels": client.list_channels(types=types)}

    @mcp.tool()
    async def slack_read_history(
        channel: str,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> dict:
        """Read recent messages from a channel or DM.

        Args:
            channel: Channel ID (C…/D…/G…).
            limit: Max messages (capped at 100 by Slack).
            cursor: Pagination cursor returned by a previous call.
        """
        return client.history(channel, limit=limit, cursor=cursor)

    @mcp.tool()
    async def slack_find_user_by_email(email: str) -> dict:
        """Look up a Slack user by email. Returns the user object (id, name, profile)."""
        return client.find_user_by_email(email)

    @mcp.tool()
    async def slack_open_dm(user: str) -> dict:
        """Open (or return) a DM channel with a user. Returns `{channel: {id: …}}`.

        Args:
            user: Slack user ID (U…). For email lookup, call `slack_find_user_by_email` first.
        """
        return client.open_dm(user)

    @mcp.tool()
    async def slack_add_reaction(channel: str, ts: str, name: str) -> dict:
        """Add an emoji reaction to a message.

        Args:
            channel: Channel ID.
            ts: Message timestamp.
            name: Emoji name without colons (e.g. `white_check_mark`).
        """
        return client.add_reaction(channel, ts, name)
