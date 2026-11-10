"""Registry declaration of the `tavily` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# tavily: web search + extract/crawl/map "for agents" (sourced answer in
# one call). keyed api_key; byo user/org AND OPEN platform key (web
# search foundation, no entry ticket). ⚠️ quota 100/month since 26/08:
# PR #407 set 0, which is NOT "small" but UNLIMITED (0 is falsy in
# access/quotas.py) — yet a crawl costs up to 20 credits per call. 100 = conservative
# guard serper-style (200), reversible in one number.
CONNECTOR = _c(
    "tavily", ["tavily"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    secret_kind="api_key", default_quota=100, platform_key_open=True,
    label="Tavily",
    help="a written, sourced answer to a question, rather than a list of "
         "links — plus site extract, crawl and map",
    href="https://app.tavily.com",
)

CATEGORY = "Web search & scraping"
PUBLISHER = "Tavily"
LOGO_DOMAIN = "tavily.com"

DESCRIPTION = (
    "A written, sourced answer to a question, rather than a list of "
    "links to sift through yourself — plus extraction, crawl and the plan (map) "
    "of a site, built for an agent. Open platform key available, with "
    "a monthly quota."
)
