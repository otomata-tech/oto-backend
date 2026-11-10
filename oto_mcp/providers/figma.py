"""Registry declaration of the `figma` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "figma", ["figma"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Figma",
    help="files, image export, comments, FigJam",
    href="https://www.figma.com",
)

CATEGORY = "Workspace"
PUBLISHER = "Figma"
LOGO_DOMAIN = "figma.com"

DESCRIPTION = (
    "A team's Figma files: read their content, export images, "
    "view comments, and FigJam."
)
