"""Registry declaration of the `sharepoint` connector — SharePoint & OneDrive, on the
Microsoft 365 account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# sharepoint: Microsoft 365 files (SharePoint sites, document libraries, OneDrive) via
# Microsoft Graph, ON BEHALF OF THE PERSON: they authorize THIS service on their
# Microsoft account, from this card, with only its permissions — the account (the vault
# row, the refresh token) is that of the `microsoft` connector. The agent sees exactly
# what the person sees; several accounts possible, chosen by `_account=` at call time.
CONNECTOR = service(
    "sharepoint",
    label="SharePoint & OneDrive",
    help="your Microsoft 365 files: sites, libraries, OneDrive — search, read, "
         "upload, with your rights",
    href="https://learn.microsoft.com/graph/api/resources/sharepoint",
)

CATEGORY = "Workspace"
LOGO_DOMAIN = "microsoft.com"

DESCRIPTION = (
    "Your Microsoft 365 files: your OneDrive, the SharePoint sites and the "
    "document libraries you have access to. Search, read a document "
    "(Word, PDF, Excel…) and upload one. You connect with your Microsoft "
    "account: the agent sees what you see, no more, no less."
)
