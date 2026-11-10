"""Registry declaration of the `kaspr` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "kaspr", ["kaspr"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=5, platform_key_open=True,
    label="Kaspr", help="enrichment", href="https://app.kaspr.io",
    # logo.dev serves a marketing banner for kaspr.io (not the brand) →
    # override with the official favicon (white K on gradient, 160×160).
    logo_url="https://www.kaspr.io/hubfs/2023%20-%20Kaspr%20Brand%20Logos/favicon.png",
)

CATEGORY = "Lead & company data"
PUBLISHER = "Kaspr"
LOGO_DOMAIN = "kaspr.io"

DESCRIPTION = (
    "Contact enrichment at Kaspr: find a person's email and direct phone "
    "number from their profile. A free platform key is available, with a "
    "limited daily quota."
)
