"""Registry declaration for the `fullenrich` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# ⚠️ **No `platform_key_open`, and this is lot L5** (ADR 0053 blueprint): the
# first connector whose access to the platform key goes through a `grants` EDGE
# and no longer through a flag + an allowlist in the vault row
# (`grants_chain.CHAIN_CONNECTORS`). The flag had only one function — preventing
# an individual grant from CLOSING the shared key for everyone (the band-aid for the
# 31/07 incident, oto-backend#245) — and that function no longer has a purpose:
# `credentials_store.platform_grant` no longer touches the vault row for this
# connector, it sets an edge. Accepted and TRUE consequence: the catalog
# stops announcing a fullenrich free tier (`public_catalog` derives it from this
# flag) — under the chain model, the platform key is granted, not
# opened. The nine other connectors with `platform_key_open` do not change
# (tripwire `tests/test_grants_l5_platform_chain.py`).
CONNECTOR = _c(
    "fullenrich", ["fullenrich"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=5,
    label="FullEnrich", help="waterfall enrichment", href="https://app.fullenrich.com",
)

CATEGORY = "Lead & company data"
PUBLISHER = "FullEnrich"
LOGO_DOMAIN = "fullenrich.com"

DESCRIPTION = (
    "\"Waterfall\" enrichment at FullEnrich: combines several "
    "data providers to find a contact's email and phone "
    "with the best success rate."
)
