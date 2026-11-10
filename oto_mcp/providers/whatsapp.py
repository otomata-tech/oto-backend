"""Registry declaration of the `whatsapp` connector — operated WhatsApp messaging.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape
shared by the six hosted connections lives with the key holder
(`providers/unipile.channel`) — here, only what distinguishes THIS one.
"""
from __future__ import annotations

from .unipile import channel

# WhatsApp: the person connects THEIR account through a hosted flow, and the tools
# `whatsapp(op=list|read)` and `whatsapp_act(op=send)` act under that identity. Derived from the shared
# messaging factory (`tools/unipile.register_messaging_tools`) — the provider's `/chats`
# API is channel-agnostic, it is the operated account's channel that decides the
# route.
#
# The card does not name our provider: what we connect is a WhatsApp account.
# The provider account (the key) is a separate connector, `unipile`.
CONNECTOR = channel(
    "whatsapp",
    hosted_channel="WHATSAPP",
    label="WhatsApp",
    help="Your WhatsApp — read your conversations and send messages. "
         "Your account connects through Unipile, our provider, which holds the session.",
    href="https://www.whatsapp.com",
)

CATEGORY = "Communication"
LOGO_DOMAIN = "whatsapp.com"
DESCRIPTION = (
    "Your WhatsApp account, connected as a linked device like "
    "WhatsApp Web: list your conversations, read a thread and send a "
    "message, under your own number."
)
