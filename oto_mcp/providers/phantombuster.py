"""Registry declaration of the `phantombuster` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "phantombuster", ["phantombuster"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Phantombuster",
    help="automation agents (launch + results)",
    href="https://phantombuster.com",
)

CATEGORY = "Outreach"
PUBLISHER = "Phantombuster"
LOGO_DOMAIN = "phantombuster.com"

DESCRIPTION = (
    "Phantombuster automation agents: launch an agent and read its "
    "results."
)
