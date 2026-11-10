"""Registry declaration of the `outlook_calendar` connector — Outlook Calendar, on the
Microsoft 365 account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# outlook_calendar: the person's Outlook calendars via Microsoft Graph, ON BEHALF OF THE
# PERSON — scope `CALENDAR` (read and write events), authorized from this card. ⚠️ Graph
# emails the attendees by itself: the tools refuse an event write that would reach
# them until the call says so (`notify_attendees`).
CONNECTOR = service(
    "outlook_calendar",
    label="Outlook Calendar",
    help="your Outlook calendars: read, create, move or cancel events — with your rights",
    href="https://learn.microsoft.com/graph/outlook-calendar-concept-overview",
)

CATEGORY = "Workspace"
LOGO_DOMAIN = "outlook.com"

DESCRIPTION = (
    "Your Outlook calendars, on your Microsoft 365 account: list the events of a period, "
    "read one, create, change or delete an event, with a Teams meeting link if you want "
    "one. Nothing is sent to attendees unless you ask for it."
)
