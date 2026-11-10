"""Registry declaration of the `google_ads` connector — Google Ads, on the Google account.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the Google services lives at the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (eighth service, 2026-10-08).
"""
from __future__ import annotations

from .google import service

# Google Ads: the person authorises THIS service on their Google account, from this
# card (scope `adwords`); reads see exactly the Google Ads accounts THEIR Google user
# can open. No developer token: since 2026-09-09 Google ignores it, and the API
# access level (test / explorer / basic / standard) is the one of the Google Cloud
# project that owns the OAuth client — an instance setting, not a customer field.
# Read-only is enforced by the oto-core client (`oto.tools.google.ads`): the scope would
# allow mutating, no mutate endpoint is ever built.
CONNECTOR = service(
    "google_ads",
    label="Google Ads",
    help="your Google Ads accounts — campaigns, ad groups, ads, keywords and their "
         "performance, read with GAQL queries; scope `adwords`, granted on your "
         "Google account. Read-only",
    href="https://ads.google.com",
)

CATEGORY = "Marketing & SEO"
LOGO_DOMAIN = "ads.google.com"
DESCRIPTION = (
    "Google Ads, on your Google account: list the ad accounts you can open, read "
    "campaigns, ad groups, ads, keywords and search terms with their performance "
    "(cost, impressions, clicks, conversions) through GAQL queries, and look up which "
    "fields a resource offers. Read-only — nothing is created, edited, paused or spent."
)
