"""Registry declaration of the `cloro` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# cloro: AI-search monitoring (ChatGPT/Gemini/Perplexity/Copilot/Grok/AI Mode) +
# Google SERP as JSON. keyed api_key, byo by default; platform mode
# GRANT-ONLY since 26/08 (#405, GTM credits) — reverses the product decision
# of signals #210-212 ("each org sets ITS key"), quota 0, never opened.
CONNECTOR = _c(
    "cloro", ["cloro"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought on credit)
    secret_kind="api_key",
    label="Cloro",
    help="what ChatGPT, Gemini or Perplexity answer about a brand — AI "
         "visibility monitoring, plus the Google SERP as JSON",
    href="https://cloro.dev",
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Cloro"
LOGO_DOMAIN = "cloro.dev"

DESCRIPTION = (
    "What ChatGPT, Gemini, Perplexity, Copilot or Grok answer when asked "
    "about a brand — AI visibility monitoring — plus the classic Google SERP "
    "as JSON. Platform access is restricted (explicit grant), while "
    "BYO stays open to any org that sets its key."
)
