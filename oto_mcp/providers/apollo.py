"""Registry declaration of the `apollo` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "apollo", ["apollo"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=20, platform_key_open=True,
    label="Apollo.io",
    help="B2B prospecting (organizations, people, job postings)",
    href="https://app.apollo.io",
)

CATEGORY = "Lead & company data"
PUBLISHER = "Apollo"
LOGO_DOMAIN = "apollo.io"

DESCRIPTION = (
    "B2B prospecting with Apollo.io: search organizations and "
    "people, read published job postings, enrich a contact or a "
    "company already found. Consumes the credits of the connected account."
)
