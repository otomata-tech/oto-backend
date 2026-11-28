"""Registry declaration of the `search_console` connector — Google Search Console, on the Google account.

Single home of its entry: `providers/__init__.py` AGGREGATES it. The shape common
to the Google services lives at the account carrier (`providers/google.service`) —
here, what distinguishes THIS one (ninth service, 2026-11-15).
"""
from __future__ import annotations

from .google import service

# Search Console by Google sign-in: the person authorises THIS service on their Google
# account, from this card (scope `webmasters.readonly`, or `webmasters` at the `full`
# level); reads see exactly the properties THEIR Google user can open in Search Console.
# No key, no service account. The other way in — a service account an admin sets once
# for the org — stays the described connector `google_search_console` (one access, one
# connector: two cards).
CONNECTOR = service(
    "search_console",
    label="Search Console (Google sign-in)",
    help="your Search Console properties — search performance (clicks, impressions, "
         "CTR, position), URL index status, sitemaps; scope `webmasters.readonly`, "
         "granted on your Google account. Read-only unless the org sets the `full` "
         "level, which submits sitemaps (scope `webmasters`)",
    href="https://search.google.com/search-console",
)

CATEGORY = "Marketing & SEO"
LOGO_DOMAIN = "search.google.com"
DESCRIPTION = (
    "Google Search Console, on your Google account: one click to sign in with Google, "
    "then read the properties you can open — search performance (clicks, impressions, "
    "CTR, position) by query, page, country, device or date, the index status of a URL, "
    "and the submitted sitemaps. Read-only by default; an org that sets the `full` "
    "access level (where the instance allows it) can also submit a sitemap, after "
    "re-authorizing the card."
)

# Access levels (`connectors/access_levels.py`), the same two as the service-account
# connector `google_search_console`: `read` (the DEFAULT — every account connected
# before the levels keeps reading, nobody reconnects) asks `webmasters.readonly`; `full`
# asks `webmasters`, which Google documents as a superset of the read-only scope
# (`scope_implies`). Widening needs a new consent from the card: a token granted at
# `read` lacks `webmasters`. `full` is also an INSTANCE choice: closed unless the
# instance sets `GOOGLE_SEARCH_CONSOLE_WRITE` (`access_levels.INSTANCE_GATES`) — closed,
# a stored `full` applies as `read` and no consent asks `webmasters`.
WEBMASTERS_READONLY = "https://www.googleapis.com/auth/webmasters.readonly"
WEBMASTERS = "https://www.googleapis.com/auth/webmasters"
ACCESS_LEVELS = {
    "levels": {"read": (WEBMASTERS_READONLY,), "full": (WEBMASTERS,)},
    "scope_implies": {WEBMASTERS: (WEBMASTERS_READONLY,)},
    "default": "read",
    "instance_gates": {"full": "GOOGLE_SEARCH_CONSOLE_WRITE"},
    "tools": {
        "search_console": {op: (WEBMASTERS_READONLY,) for op in (
            "list_sites", "query_search_analytics", "inspect_url", "list_sitemaps",
            "get_sitemap")},
        "search_console_act": {"add_sitemap": (WEBMASTERS,)},
    },
}
