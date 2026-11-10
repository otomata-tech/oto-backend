"""Registry declaration of the `teams` connector — Microsoft Teams, on the Microsoft 365
account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# teams: the person's teams, channels and chats via Microsoft Graph, ON BEHALF OF THE
# PERSON — scopes `TEAMS` (teams and channels, posting in a channel, chats), authorized
# from this card. READING a channel's messages is an administrator tier
# (`TEAMS_ADMIN`, `auth/microsoft.SERVICE_ADMIN_TIER`): checked at use, the card says
# when it is missing.
CONNECTOR = service(
    "teams",
    label="Teams",
    help="your Microsoft Teams: teams, channels and chats — read and post, with your rights",
    href="https://learn.microsoft.com/graph/teams-concept-overview",
)

CATEGORY = "Communication"
LOGO_DOMAIN = "microsoft.com"

DESCRIPTION = (
    "Your Microsoft Teams, on your Microsoft 365 account: your teams and their channels, "
    "your chats; read a chat, post or reply in a channel or a chat. Reading a channel's "
    "messages needs your Microsoft 365 administrator's approval, once for the whole "
    "organization."
)
