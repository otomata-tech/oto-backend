"""Registry declaration of the `klaviyo` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# klaviyo: email & SMS marketing — the customer relationship side of the API:
# account, profiles, lists, segments, campaigns, flows, metrics, events,
# metric aggregates and the campaign / flow values reports (read); profiles,
# list membership, events and marketing consent (write). NOTHING sends a
# campaign or creates/edits a campaign, flow or template.
# The client lives in the connector library (`oto.tools.klaviyo`), the tools in
# `tools/klaviyo.py` (account, profiles, lists, segments, consent) and
# `tools/klaviyo_marketing.py` (campaigns, flows, metrics, events, reports); the
# shared base in `tools/klaviyo_socle.py`.
#
# **byo-only, by nature**: a private key belongs to ONE Klaviyo account and
# carries the scopes chosen when it was created (they cannot be edited
# afterwards). A platform key would read and write someone else's customers.
#
# ⚠️ Four writes may reach real people: adding profiles to a list and recording
# an event start the flows they trigger; subscribing changes consent (and a
# double opt-in list emails a confirmation); unsubscribing without a list is
# global. The tools return a PREVIEW for those until `confirm=True`.
CONNECTOR = _c(
    "klaviyo", ["klaviyo"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    modules=("klaviyo", "klaviyo_marketing"),
    label="Klaviyo",
    help="email & SMS marketing: profiles, lists, segments, consent, events, "
         "campaign and flow reports — never sends a campaign",
    href="https://www.klaviyo.com",
    credential_fields=(
        CredentialField(
            "key", "Klaviyo private API key", secret=True,
            help="Klaviyo → Settings → Account → API keys → \"Create Private API "
                 "Key\" (`pk_…`). Pick the scopes when creating it — they cannot "
                 "be added later: accounts:read (needed by \"test connection\"), "
                 "profiles, lists, segments, campaigns, flows, metrics, events "
                 "(read), plus profiles:write, lists:write, events:write and "
                 "subscriptions:write for the writes. The key reaches one "
                 "Klaviyo account."),
    ),
)

CATEGORY = "Marketing & SEO"
PUBLISHER = "Klaviyo"
LOGO_DOMAIN = "klaviyo.com"

DESCRIPTION = (
    "Klaviyo email and SMS marketing, from your assistant: find and update "
    "customer profiles, manage list membership and marketing consent, record "
    "events, and read campaigns, flows, metrics and their performance reports. "
    "It never sends a campaign. Each person or organization connects its own "
    "private API key — no shared platform key."
)
