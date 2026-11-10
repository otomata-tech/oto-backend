"""Registry declaration of the `gmail` connector — Gmail, on the Google account.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the six Google services lives with the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (split of 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Gmail: the person authorizes THIS service on their Google account, from this
# card, with only its own scopes — the account (the vault row, the refresh token) is
# the one of the `google` connector, shared with the five other services.
CONNECTOR = service(
    "gmail",
    label="Gmail",
    help="your Gmail inbox — search, read, draft, send, archive; scope `gmail.modify`, granted on your Google account",
    href="https://mail.google.com",
)

CATEGORY = "Communication"
LOGO_DOMAIN = "gmail.com"
DESCRIPTION = (
    "Your Gmail inbox, on your Google account: search and read messages, draft or send, archive, move to trash, read an attachment. A consent that only asks for the Gmail scope."
)
