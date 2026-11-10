"""Registry declaration of the `sheets` connector — Google Sheets, on the Google account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the six Google services lives with the account carrier (`providers/google.service`) —
here, what distinguishes THIS ONE (split of 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Sheets: the person authorizes THIS service on their Google account, from this
# card, with its own scopes only — the account (the vault row, the refresh token) is
# that of the `google` connector, shared with the five other services.
CONNECTOR = service(
    "sheets",
    label="Google Sheets",
    help="your spreadsheets — read, write, create; `spreadsheets` scope, granted on your Google account",
    href="https://docs.google.com/spreadsheets",
)

CATEGORY = "Workspace"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Google Sheets, on your Google account: read and write a spreadsheet's cells, create an empty one, copy a template tab and format cells, columns and drop-downs. A consent that only asks for the Sheets scope."
)
