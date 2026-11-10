"""Registry declaration of the `serper` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "serper", ["serper"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=200, platform_key_open=True,
    # Three brands one character apart (serper/serpapi/searchapi) and four
    # neighbouring scopes (cloro/firecrawl/tavily/brightdata): since 2026-09-02 each help
    # says WHAT DISTINGUISHES IT from the others, not what it is.
    label="Serper",
    help="Google as JSON — web, images, Maps and reviews, Lens, plus single-page "
         "scraping; oto's default general-purpose engine",
    href="https://serper.dev",
)

CATEGORY = "Web search & scraping"
PUBLISHER = "Serper"
LOGO_DOMAIN = "serper.dev"

DESCRIPTION = (
    "Google as JSON — web search, images, Google Maps and its reviews, Lens, "
    "plus single-page scraping — oto's default general-purpose search "
    "engine. Shared platform key available, with a daily quota."
)
