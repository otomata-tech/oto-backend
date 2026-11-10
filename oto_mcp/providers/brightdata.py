"""Registry declaration of the `brightdata` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# brightdata: scraping & SERP via the Bright Data proxy network. EMPTY SHELL —
# connector wired (platform key + quota) but products (SERP/Unlocker/Datasets)
# not yet implemented (tools/brightdata.py exposes no tool for now).
CONNECTOR = _c(
    "brightdata", ["brightdata"], auth_modes={"byo_user", "byo_org", "platform"},
    keyed=True, secret_kind="api_key",
    default_quota=50, label="Bright Data",
    help="scraping & SERP via proxy (empty shell — to be implemented)",
    href="https://brightdata.com",
)

CATEGORY = "Web search & scraping"
PUBLISHER = "Bright Data"
LOGO_DOMAIN = "brightdata.com"

DESCRIPTION = (
    "Scraping and SERP via the Bright Data proxy network — the card exists, "
    "the tools (SERP, Unlocker, Datasets) are not yet built."
)
