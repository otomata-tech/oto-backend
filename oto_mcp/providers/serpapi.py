"""Registry declaration of the `serpapi` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# serpapi: multi-engine search (full scope — all Google verticals
# + Bing/YouTube/Walmart/Amazon/eBay/… + Google Jobs). keyed api_key, platform-
# eligible (platform key + daily quota, like serper).
CONNECTOR = _c(
    "serpapi", ["serpapi"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=200, platform_key_open=True,
    label="SerpApi",
    help="to query an engine OTHER than Google — Bing, YouTube, Amazon, "
         "Walmart, eBay, Jobs, Scholar…",
    href="https://serpapi.com",
)

CATEGORY = "Web search & scraping"
PUBLISHER = "SerpApi"
LOGO_DOMAIN = "serpapi.com"

DESCRIPTION = (
    "Query an engine OTHER than Google: Bing, YouTube, Amazon, Walmart, "
    "eBay, job postings (Jobs), Google Scholar… the full multi-"
    "engine scope of SerpApi. Shared platform key available, with a daily "
    "quota."
)
