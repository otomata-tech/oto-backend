"""Registry declaration for the `hithorizons` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "hithorizons", ["hithorizons"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought per credit)
    secret_kind="api_key", label="HitHorizons",
    help="European company data (search + details)",
    href="https://www.hithorizons.com",
)

CATEGORY = "Lead & company data"
PUBLISHER = "HitHorizons"
LOGO_DOMAIN = "hithorizons.com"

DESCRIPTION = (
    "European company data from HitHorizons: search and detailed "
    "profile."
)
