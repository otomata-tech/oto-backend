"""Registry declaration of the `outlook` connector — Outlook mail, on the Microsoft 365
account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# outlook: the person's Outlook mailbox via Microsoft Graph, ON BEHALF OF THE PERSON —
# they authorize THIS service (scopes `MAIL`: read, draft, move, send) on their
# Microsoft account, from this card. Every write is a draft first; sending is explicit.
CONNECTOR = service(
    "outlook",
    label="Outlook",
    help="your Outlook mailbox: search, read, draft, send, archive — with your rights",
    href="https://learn.microsoft.com/graph/outlook-mail-concept-overview",
)

CATEGORY = "Communication"
LOGO_DOMAIN = "outlook.com"

DESCRIPTION = (
    "Your Outlook mailbox, on your Microsoft 365 account: search and read messages, "
    "read an attachment, write a draft or a reply, send it, archive, move or trash a "
    "message. A consent that only asks for the mail permissions."
)
