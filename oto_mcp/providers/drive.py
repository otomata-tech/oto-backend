"""Registry declaration for the `drive` connector — Google Drive, on the Google account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the six Google services lives with the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (split of 2026-09-26).
"""
from __future__ import annotations

from .google import service

# Google Drive: the person authorizes THIS service on their Google account, from this
# card, with only its scopes — the account (the vault row, the refresh token) is
# that of the `google` connector, shared with the five other services.
CONNECTOR = service(
    "drive",
    label="Google Drive",
    help="your Drive files — list, read, organize, share, delete; scope `drive`, granted on your Google account",
    href="https://drive.google.com",
)

CATEGORY = "Workspace"
LOGO_DOMAIN = "google.com"
DESCRIPTION = (
    "Your Google Drive, on your Google account: list and read files and folders, move or delete them, control who has access. A consent that only asks for the Drive scope."
)
