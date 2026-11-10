"""Registry declaration of the `firecrawl` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# firecrawl: a URL → clean markdown (JS rendering, nav removed); map/crawl for
# a whole site, search for web + content. keyed api_key, byo by default
# (credit billing at the vendor); platform key GRANT-ONLY since
# 26/08 (#405, credits GTM).
CONNECTOR = _c(
    "firecrawl", ["firecrawl"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought on credit)
    secret_kind="api_key",
    label="Firecrawl",
    help="convert a whole site into clean markdown to hand to an agent — "
         "scrape, crawl, map; not a search engine",
    href="https://firecrawl.dev",
)

CATEGORY = "Web search & scraping"
PUBLISHER = "Firecrawl"
LOGO_DOMAIN = "firecrawl.dev"

DESCRIPTION = (
    "Convert a page or a whole site into clean markdown to hand to an "
    "agent — not a search engine. Scrape a URL, crawl or map a complete "
    "site, or search the web with the page content already extracted. "
    "Platform access reserved (explicit grant), BYO remains open."
)
