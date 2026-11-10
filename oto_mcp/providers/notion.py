"""Registry declaration of the `notion` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "notion", ["notion"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Notion",
    help="pages, databases, blocks (read + write)",
    href="https://www.notion.so",
)

CATEGORY = "Workspace"
PUBLISHER = "Notion"
LOGO_DOMAIN = "notion.so"

DESCRIPTION = (
    "A team's Notion workspace: pages, databases and blocks, with "
    "read and write access."
)
