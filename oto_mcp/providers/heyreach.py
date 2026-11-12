"""Registry declaration of the `heyreach` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# heyreach: LinkedIn outreach automation for agencies and sales teams — campaigns
# run from a pool of LinkedIn sender accounts, their leads and lead lists, the
# unified LinkedIn inbox, statistics and webhooks. A neighbour of `waalaxy` and
# `lemlist` by trade. The client lives in the connector library
# (`oto.tools.heyreach.HeyReachClient`, the client of the `heyreach` description),
# the tools in `tools/heyreach.py` (the pair `heyreach` reads, `heyreach_act` acts).
#
# keyed `api_key`: one key per HeyReach workspace, sent as `X-API-KEY`; it acts on
# every sender account, campaign, list and conversation of that workspace. Keys
# never expire but can be deleted or deactivated.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): the senders are the
# customer's own LinkedIn accounts, there can be no shared oto key.
CONNECTOR = _c(
    "heyreach", ["heyreach"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="HeyReach",
    help="LinkedIn outreach: campaigns and their leads, lead lists, the LinkedIn "
         "inbox, statistics and webhooks",
    href="https://www.heyreach.io",
    credential_fields=(
        CredentialField(
            "key", "HeyReach API key", secret=True,
            help="HeyReach → Integrations → HeyReach API: copy the workspace's API key. "
                 "It acts on every sender account, campaign and conversation of that "
                 "workspace; it never expires but can be deactivated there."),
    ),
)

CATEGORY = "Prospecting"
PUBLISHER = "HeyReach"
LOGO_DOMAIN = "heyreach.io"

DESCRIPTION = (
    "LinkedIn outreach with HeyReach: read campaigns, their leads and statistics, "
    "lead lists and the unified LinkedIn inbox of your sender accounts; add leads to "
    "a campaign or a list, pause or resume a campaign, stop a lead, reply in a "
    "conversation, tag leads, and set webhooks that wake an agent on replies. Your "
    "own workspace API key."
)
