"""Registry declaration of the `zerobounce` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "zerobounce", ["zerobounce"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought by the credit)
    secret_kind="api_key", label="ZeroBounce",
    help="email deliverability verification", href="https://www.zerobounce.net",
)

CATEGORY = "Lead & company data"
PUBLISHER = "ZeroBounce"
LOGO_DOMAIN = "zerobounce.net"

DESCRIPTION = (
    "Check the deliverability of an email address before using it, with "
    "ZeroBounce."
)
