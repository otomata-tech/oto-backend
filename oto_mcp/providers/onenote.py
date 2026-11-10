"""Registry declaration of the `onenote` connector — OneNote, on the Microsoft 365
account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# onenote: the OneNote notebooks the person reaches via Microsoft Graph, ON BEHALF OF THE
# PERSON — scope `NOTES` (`Notes.ReadWrite.All`: her notebooks, her teams' and SharePoint
# sites' ones; no administrator approval), authorized from this card. Read, create and
# add to pages; nothing deletes.
CONNECTOR = service(
    "onenote",
    label="OneNote",
    help="your OneNote notebooks, and your teams' ones: read pages, write and add to them "
         "— with your rights",
    href="https://learn.microsoft.com/graph/integrate-with-onenote",
)

CATEGORY = "Workspace"
LOGO_DOMAIN = "onenote.com"

DESCRIPTION = (
    "Your OneNote notebooks, on your Microsoft 365 account — your own, your teams' and "
    "your SharePoint sites' ones: browse notebooks, sections and pages, read a page, "
    "create a page or add to one, create a section or a notebook."
)
