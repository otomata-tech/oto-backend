"""Registry declaration of the `chat` connector — Google Chat, on the Google account.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the six Google services lives with the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (split of 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Chat: the person authorizes THIS service on their Google account, from this
# card, with only its own scopes — the account (the vault row, the refresh token) is
# the one of the `google` connector, shared with the five other services.
CONNECTOR = service(
    "chat",
    label="Google Chat",
    help="your Google Chat spaces — read spaces and messages, post; scopes `chat.spaces.readonly` + `chat.messages`, granted on your Google account",
    href="https://chat.google.com",
)

CATEGORY = "Communication"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Chat, on your Google account: list the spaces (rooms and DMs) you belong to, read their messages and post to them. A consent that only asks for the Chat scopes."
)
