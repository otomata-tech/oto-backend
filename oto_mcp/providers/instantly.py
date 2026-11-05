"""Registry declaration of the `instantly` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# instantly: cold email outreach — campaigns and their analytics, leads and lead
# lists, the Unibox (replies), sending accounts, the block list and webhooks. A
# neighbour of `lemlist` by trade. The client lives in the connector library
# (`oto.tools.instantly.InstantlyClient`, the client of the `instantly`
# description), the tools in `tools/instantly.py` (the pair `instantly` reads,
# `instantly_act` acts).
#
# keyed `api_key`: one workspace API key (API V2), sent as `Authorization:
# Bearer`. A key carries SCOPES chosen at its creation: a read-only key
# (`all:read`) serves `instantly` and refuses every `instantly_act` op with a
# 403 that names the missing scope.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): the campaigns, leads
# and mailboxes are the customer's own, there can be no shared oto key.
CONNECTOR = _c(
    "instantly", ["instantly"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Instantly",
    help="cold email outreach: campaigns and analytics, leads, replies, webhooks",
    href="https://instantly.ai",
    credential_fields=(
        CredentialField(
            "key", "Instantly API key", secret=True,
            help="Instantly → Settings → Integrations → API keys → Create API key (API V2). "
                 "Scopes: all:all to read and act, all:read to read only (actions are then "
                 "refused). The key reaches one workspace."),
    ),
)

CATEGORY = "Prospecting"
PUBLISHER = "Instantly"
LOGO_DOMAIN = "instantly.ai"

DESCRIPTION = (
    "Cold email outreach with Instantly: read campaigns and their analytics, leads, "
    "lead lists and the Unibox replies; add leads to a campaign or a list, update a "
    "lead or its interest status, start or pause a campaign, reply to an email, block "
    "an address, and register webhooks that wake agents on replies. A workspace API "
    "key with the scopes to use (all:read reads only)."
)
