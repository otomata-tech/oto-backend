"""Registry declaration of the `searchapi` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# searchapi: multi-engine search via SearchApi.io (Google verticals +
# YouTube/Bing/Amazon/… + jobs/news/maps/scholar). keyed api_key, platform-
# eligible (platform key + daily quota, like serper/serpapi). Self-contained
# HTTP client (no oto-core dep).
CONNECTOR = _c(
    "searchapi", ["searchapi"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=200, platform_key_open=True,
    label="SearchApi",
    help="same multi-engine scope as SerpApi (Google, YouTube, Bing, jobs, "
         "news, maps, scholar) — set it up if your key is with SearchApi",
    href="https://www.searchapi.io",
)

CATEGORY = "Web search & scraping"
PUBLISHER = "SearchApi"
LOGO_DOMAIN = "searchapi.io"

DESCRIPTION = (
    "The same multi-engine scope as SerpApi — Google, YouTube, Bing, "
    "job postings, news, maps, Google Scholar — to set up if your key "
    "is with SearchApi.io rather than SerpApi. Shared platform key "
    "available, with a daily quota."
)
