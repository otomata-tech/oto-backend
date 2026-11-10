"""Registry declaration of the `meta_ads` connector — the Facebook and
Instagram campaigns of a Meta ad account, through the Marketing API. READ-ONLY.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# Same family as `instagram_meta` (the instance's Meta application, credentials
# set at the platform tier, flow hosted by oto), but NOT the same product:
# Facebook Login for Business on graph.facebook.com, a `config_id` instead of a
# list of permissions, and a system-user token (BISU) that does not expire
# — hence no renewal pass.
#
# ⚠️ Without App Review ("Advanced" access), the application stays on
# "Standard" access: it works, but is heavily rate-limited. The card says so.
CONNECTOR = _c(
    "meta_ads", ["meta_ads"],
    auth_modes={"byo_user"},
    # Consent comes from the person's Facebook account, not from their org.
    personal_session=True, secret_kind="oauth",
    label="Meta Ads",
    help="Your Facebook & Instagram ad accounts: campaigns, ad sets, ads and their "
         "performance (spend, reach, clicks, conversions). You authorize oto on "
         "Facebook and pick the ad accounts. Read-only.",
    href="https://www.facebook.com/business/ads",
)

CATEGORY = "Marketing & SEO"
# Who receives the call: Meta — its official API, its consent dialog.
PUBLISHER = "Meta"
LOGO_DOMAIN = "facebook.com"

DESCRIPTION = (
    "Facebook and Instagram advertising through Meta's official Marketing API: "
    "list the ad accounts you granted, browse campaigns, ad sets and ads, and pull "
    "performance insights (spend, impressions, reach, clicks, CPC, CTR, "
    "conversions) by day, placement, age, gender or country. Read-only — nothing "
    "is created, edited, paused or spent."
)
