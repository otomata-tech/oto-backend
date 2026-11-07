"""Registry declaration of the `search_console` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# search_console: same credential as `google_analytics`, a SERVICE ACCOUNT KEY and
# not the OAuth of the `google` connector: added as a user of a property, the
# service account reads it without anyone's Google account, and is cut off by
# removing it. ONE secret field, `whitespace_significant` for the same reason (the
# PEM key contains spaces). byo_org first: an org's agents all read one site.
CONNECTOR = _c(
    "search_console", ["search_console"], auth_modes={"byo_org", "byo_user"},
    secret_kind="fields", label="Google Search Console",
    help="Google search performance and indexing status of your sites, plus sitemap "
         "submission, via a service account",
    href="https://search.google.com/search-console", credential_fields=(
        CredentialField(
            "service_account_json", "Service account JSON key", secret=True,
            whitespace_significant=True,
            help="Google Cloud → enable the Google Search Console API, then IAM → "
                 "Service accounts → Keys → Add key → JSON: paste the ENTIRE contents "
                 "of the file. Then, in Search Console, add the service account's "
                 "email as a user of the property (Settings → Users and permissions)."),
    ),
)

CATEGORY = "Marketing"
PUBLISHER = "Google"
LOGO_DOMAIN = "search.google.com"

DESCRIPTION = (
    "What Google knows about your sites: clicks, impressions, position and queries "
    "per page, whether a URL is indexed and why not, and your sitemaps. Access via "
    "a service account added as a user in Search Console — no personal Google "
    "account is connected."
)
