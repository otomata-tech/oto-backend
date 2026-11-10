"""Registry declaration of the `powerbi` connector — Power BI, on the Microsoft 365
account.

Sole home of its entry: `providers/__init__.py` AGGREGATES it. The shape common to
the Microsoft services lives with the account carrier (`providers/microsoft.service`) —
here, what distinguishes THIS one.
"""
from __future__ import annotations

from .microsoft import service

# powerbi: the workspaces, reports and semantic models the person reaches in Power BI,
# ON BEHALF OF THE PERSON — scopes `POWERBI` (Workspace, Report, Dataset, all .Read.All),
# a resource of its own outside Graph (`auth/microsoft.SERVICE_RESOURCE`), authorized
# from this card. Read only: list, query a model in DAX, read measure definitions.
CONNECTOR = service(
    "powerbi",
    label="Power BI",
    help="your Power BI workspaces, reports and models: list them, query a model in DAX, "
         "read its measures — read only, with your rights",
    href="https://learn.microsoft.com/rest/api/power-bi/",
)

CATEGORY = "Product & data"
LOGO_DOMAIN = "powerbi.com"

DESCRIPTION = (
    "Your Power BI, on your Microsoft 365 account, read only: workspaces, reports and "
    "their pages, semantic models; run a DAX query on a model; read the definition of "
    "its measures (the DAX formula needs write permission on the model)."
)
