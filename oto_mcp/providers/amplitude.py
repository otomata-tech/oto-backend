"""Registry declaration of the `amplitude` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# amplitude: product analytics, READ ONLY — taxonomy, Dashboard REST queries
# (segmentation, funnels, retention, active users, sessions), saved charts,
# user lookup, behavioral cohorts. byo only: it is the customer's product data.
# THREE fields: the project's API key AND secret key (the API key alone reads
# nothing — it is the public key of the SDK snippet), and the region. One key
# pair = one project. us/eu are two deployments and a key is unknown to the
# other; the region is a closed set (`choices`), never a free URL — so no
# destination is ever typed in.
CONNECTOR = _c(
    "amplitude", ["amplitude"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="Amplitude",
    help="product analytics, read only: events and properties, segmentation, "
         "funnels, retention, saved charts, users, cohorts",
    href="https://amplitude.com", modules=("amplitude", "amplitude_query"),
    credential_fields=(
        CredentialField(
            "api_key", "API key", secret=True,
            help="Amplitude → Settings → Organization settings → Projects → "
                 "<your project> → General. One key pair = one project."),
        CredentialField(
            "secret_key", "Secret key", secret=True,
            help="Same page as the API key. Required: the API key alone is "
                 "refused by every read endpoint."),
        CredentialField(
            "region", "Data region", secret=False, required=False,
            choices=("us", "eu"),
            help="Where the project's data lives: empty or « us » (amplitude.com), "
                 "« eu » (analytics.eu.amplitude.com). A key is unknown to the "
                 "other region."),
    ),
)

CATEGORY = "Dev"
PUBLISHER = "Amplitude"
LOGO_DOMAIN = "amplitude.com"

DESCRIPTION = (
    "Product analytics from Amplitude, read only: the project's events and "
    "properties, segmentation, funnels, retention and active users, saved "
    "charts as the team sees them, a user's event stream, and cohorts. Each "
    "person or organization connects its own project key pair (API key + "
    "secret key) and region — no shared platform key."
)
