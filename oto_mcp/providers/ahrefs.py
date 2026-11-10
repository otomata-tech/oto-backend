"""Registry declaration of the `ahrefs` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# ahrefs: SEO — backlinks, keywords, rank tracking, technical audits,
# brand visibility on AI chatbots, on-site analytics, GSC, social
# publishing. keyed api_key (Bearer), byo-only (no platform key):
# an Ahrefs seat is expensive and per subscription.
CONNECTOR = _c(
    "ahrefs", ["ahrefs"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Ahrefs",
    help="SEO — backlinks, keywords, rank tracking, technical audits, "
         "brand visibility on AI chatbots, analytics, GSC, social",
    href="https://ahrefs.com",
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Ahrefs"
LOGO_DOMAIN = "ahrefs.com"

DESCRIPTION = (
    "A site's SEO as seen by Ahrefs: backlinks, ranking keywords, rank "
    "tracking, technical audits, brand visibility in AI chatbot "
    "answers, on-site analytics, Search Console and social publishing. Byo "
    "only — an Ahrefs seat is an expensive, named subscription, no "
    "platform key."
)
