"""Registry declaration of the `lusha` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# lusha: contact search + reveal (emails/phones), byo by default;
# GRANT-ONLY platform key since 26/08 (#405). Auth = flat `api_key` header
# (not OAuth), only 1
# endpoint wired for now (search-and-enrich).
CONNECTOR = _c(
    "lusha", ["lusha"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought by credit)
    secret_kind="api_key",
    label="Lusha", help="contact search + reveal (emails/phones)",
    publisher="Lusha", href="https://www.lusha.com",
)

CATEGORY = "Lead & company data"
LOGO_DOMAIN = "lusha.com"

DESCRIPTION = (
    "Contact search and reveal at Lusha: find a person's email and "
    "phone from their profile."
)
