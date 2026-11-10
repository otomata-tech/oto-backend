"""Registry declaration of the `telegram` connector — operated Telegram messaging.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape
shared by the six hosted connections lives with the key holder
(`providers/unipile.channel`) — here, only what distinguishes THIS one.
"""
from __future__ import annotations

from .unipile import channel

# Telegram: the person connects THEIR account through a hosted flow, and the tools
# `telegram(op=list|read)` and `telegram_act(op=send)` act under that identity. Derived from the shared
# messaging factory (`tools/unipile.register_messaging_tools`) — the provider's
# `/chats` API is channel-agnostic, the channel of the operated account decides the
# route.
#
# The card does not name our provider: what we connect is a Telegram account.
# The provider account (the key) is a separate connector, `unipile`.
CONNECTOR = channel(
    "telegram",
    hosted_channel="TELEGRAM",
    label="Telegram",
    help="Your Telegram — read your conversations and send messages. "
         "Your account connects through Unipile, our provider, which holds the session.",
    href="https://telegram.org",
)

CATEGORY = "Communication"
LOGO_DOMAIN = "telegram.org"
DESCRIPTION = (
    "Your Telegram account: list your conversations, read a thread and send "
    "a message, under your own account."
)
