"""Registry declaration of the `posthog` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# posthog: product analytics — HogQL, events, persons, accounts (groups),
# insights, feature flags, session recordings. byo-only.
# THREE fields: the PERSONAL key `phx_…` (the PROJECT key `phc_…`, the one
# PostHog puts forward most, is refused by the read API — the
# client rejects it on entry rather than leaving an unreadable 401); the
# regional `host`, since us/eu are two distinct deployments and a key from
# one is unknown to the other; and an optional `project_id` that pins the
# key to ONE project (otherwise discovered from the key).
CONNECTOR = _c(
    "posthog", ["posthog"], auth_modes={"byo_user", "byo_org"},
    secret_kind="fields", label="PostHog",
    help="product analytics — HogQL queries, events, persons, insights, "
         "feature flags, session recordings",
    href="https://posthog.com", credential_fields=(
        CredentialField("api_key", "Personal API key (phx_…)", secret=True,
                        help="PostHog → Settings → Personal API keys. NOT the "
                             "project key `phc_…` from the JS snippet, which is refused here."),
        CredentialField("host", "Region / instance", secret=False, required=False,
                        help="https://us.posthog.com (default) or "
                             "https://eu.posthog.com, or your instance's URL"),
        CredentialField("project_id", "Default project", secret=False,
                        required=False,
                        help="pins the key to ONE project; otherwise resolved "
                             "automatically from the key"),
    ),
)

CATEGORY = "Product & data"
PUBLISHER = "PostHog"
LOGO_DOMAIN = "posthog.com"

DESCRIPTION = (
    "Product usage reported by PostHog: HogQL queries, events, "
    "persons and accounts (groups), insights and feature flags, session "
    "recordings. Personal key only (`phx_…`) — the project key put "
    "forward by PostHog has no read access to this data."
)
