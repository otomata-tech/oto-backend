"""Registry declaration for the `zapier` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "zapier", ["zapier"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Zapier",
    help="automation — exposed actions (AI Actions) + execution",
    href="https://actions.zapier.com",
)

CATEGORY = "AI & automation"
PUBLISHER = "Zapier"
LOGO_DOMAIN = "zapier.com"

DESCRIPTION = (
    "The AI Actions exposed by a Zapier account: list the available "
    "actions and run them."
)
