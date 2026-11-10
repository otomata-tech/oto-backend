"""Registry declaration of the `calendar` connector — Google Calendar, on the Google account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the six Google services lives at the account carrier (`providers/google.service`) —
here, what distinguishes THIS ONE (split of 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Calendar: the person authorises THIS service on their Google account, from this
# card, with only its scopes — the account (the vault row, the refresh token) is
# that of the `google` connector, shared with the other five services.
CONNECTOR = service(
    "calendar",
    label="Google Calendar",
    help="your calendar — list calendars, read, create, edit, delete an event; `calendar` scope, granted on your Google account",
    href="https://calendar.google.com",
)

CATEGORY = "Workspace"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Calendar, on your Google account: list your calendars, read and create events, edit or delete them. A consent that only requests the Calendar scope."
)
