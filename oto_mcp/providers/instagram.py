"""Registry declaration for the `instagram` connector — operated Instagram messaging.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The form
shared by the six hosted connections lives with the key holder
(`providers/unipile.channel`) — here, what distinguishes THIS one.
"""
from __future__ import annotations

from .unipile import channel

# Instagram: the person connects THEIR account through a hosted flow, and the tools
# `instagram(op=list|read)` and `instagram_act(op=send)` act under that identity. Derived from the
# shared messaging factory (`tools/unipile.register_messaging_tools`) — the provider's
# `/chats` API is channel-agnostic, it is the operated account's channel that decides
# the route.
#
# The card does not name our provider: what is being connected is an Instagram
# account. The provider account (the key) is a separate connector, `unipile`.
CONNECTOR = channel(
    "instagram",
    hosted_channel="INSTAGRAM",
    label="Instagram",
    help="Your Instagram DMs — read and send messages. "
         "Your account connects through Unipile, our provider, which holds the session.",
    href="https://www.instagram.com",
)

CATEGORY = "Communication"
LOGO_DOMAIN = "instagram.com"
DESCRIPTION = (
    "The private messages of your Instagram account: list your conversations, "
    "read a thread and send a message. DMs only — no feed, no "
    "stories, no posts."
)
