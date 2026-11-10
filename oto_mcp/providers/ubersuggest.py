"""Registry declaration of the `ubersuggest` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# ubersuggest: SEO — keyword research, domain and page traffic, backlinks, site
# audits, rank-tracking projects, Content Studio articles. ON BEHALF OF THE PERSON:
# each one signs in with their own Ubersuggest account (OAuth, public client
# registered dynamically by `auth/ubersuggest.py`), so the data and the limits are
# those of THEIR plan. The host is fixed: no egress guard to set.
CONNECTOR = _c(
    "ubersuggest", ["ubersuggest"],
    auth_modes={"byo_user"},
    # Consent comes from the person's Ubersuggest account, not from their org.
    personal_session=True, secret_kind="oauth",
    label="Ubersuggest",
    help="SEO with your Ubersuggest account — keywords, traffic, backlinks, site "
         "audits, rank tracking",
    href="https://neilpatel.com/ubersuggest/",
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Ubersuggest"
LOGO_DOMAIN = "neilpatel.com"

DESCRIPTION = (
    "SEO data from your own Ubersuggest account: keyword volume, difficulty and "
    "ideas, a domain's or a page's traffic and ranking keywords, competitors, "
    "backlinks, site and PageSpeed audits, keyword lists, rank-tracking projects "
    "and Content Studio articles. You sign in with Ubersuggest: what comes back, "
    "and how much, depends on your plan."
)
