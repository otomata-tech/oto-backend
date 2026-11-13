"""Registry declaration of the `google_search_console` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# google_search_console: Search Console via a SERVICE ACCOUNT KEY,
# served by the GENERIC READER of its description (`tools/decrits.py`): no
# `tools/google_search_console.py`, the library's engine runs the description
# (`oto.connectors.engine`, auth `oauth2_jwt_bearer`). Label, namespace, modes and
# the credential field are read in the description: `byo_org` only, the founding
# case being an org whose agents all read the same properties. The key's JSON file
# is kept whole under the description's field name (`service_account_key`),
# `secret_kind="fields"` like `google_analytics`. Two access levels, read in the
# description (`connectors/access_levels.py`): `read` by default (every key installed
# before 13/11/2026 stays there), `full` adds submitting a sitemap.
CONNECTOR = _decrit(
    "google_search_console",
    help="Search Console: properties shared with a service account, search "
         "performance, URL index status, sitemaps; read-only unless the org sets the "
         "`full` level, which submits sitemaps",
    href="https://search.google.com/search-console",
    key_help="JSON key of the service account; add its email as a user of the "
             "Search Console property",
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Google"
LOGO_DOMAIN = "search.google.com"

DESCRIPTION = (
    "Google Search Console: the properties shared with a service account, their "
    "search performance (clicks, impressions, CTR, position), the index status "
    "of a URL and the submitted sitemaps. Read-only by default; an org that sets "
    "the `full` access level can also submit a sitemap. Access via a service "
    "account added as a user of each property — no personal Google account is "
    "connected."
)
