"""Registry declaration of the `search_console` connector — Google Search Console, on the Google account.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the Google services lives at the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (ninth service, 2026-11-15).
"""
from __future__ import annotations

from .google import service

# Search Console by Google sign-in: the person authorises THIS service on their Google
# account, from this card (scope `webmasters.readonly`); reads see exactly the
# properties THEIR Google user can open in Search Console. No key, no service account.
# The other way in — a service account an admin sets once for the org — stays the
# described connector `google_search_console` (one access, one connector: two cards).
CONNECTOR = service(
    "search_console",
    label="Search Console (Google sign-in)",
    help="your Search Console properties — search performance (clicks, impressions, "
         "CTR, position), URL index status, sitemaps; scope `webmasters.readonly`, "
         "granted on your Google account. Read-only",
    href="https://search.google.com/search-console",
)

CATEGORY = "Marketing & SEO"
LOGO_DOMAIN = "search.google.com"
DESCRIPTION = (
    "Google Search Console, on your Google account: one click to sign in with Google, "
    "then read the properties you can open — search performance (clicks, impressions, "
    "CTR, position) by query, page, country, device or date, the index status of a URL, "
    "and the submitted sitemaps. Read-only — nothing is submitted or changed."
)
