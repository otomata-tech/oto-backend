"""Registry declaration of the `heyreach` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# heyreach: LinkedIn outreach automation — campaigns run from a pool of LinkedIn
# sender accounts, their leads and lead lists, the unified LinkedIn inbox,
# statistics and webhooks. A neighbour of `waalaxy` by trade. Served by the GENERIC
# READER of its description (`tools/decrits.py`): no `tools/heyreach.py`, the
# library's engine runs the description (auth `api_key`, header `X-API-KEY`).
# Label, namespace, modes and the credential field are read in the description:
# `byo_user` and `byo_org`, no platform key (the senders are the customer's own
# LinkedIn accounts). The key is kept under `key`, resolved by
# `access.resolve_api_key`.
#
# The pair splits on the description's classes: `heyreach` reads, `heyreach_act`
# holds every write; what starts outreach, replies or posts to an outside address
# is `send`, what stops a lead or drops tags or a webhook for good is `destroy`
# (derived into `tools/effets.py`, never written there).
CONNECTOR = _decrit(
    "heyreach",
    help="LinkedIn outreach: campaigns and their leads, lead lists, the LinkedIn "
         "inbox, statistics and webhooks",
    href="https://www.heyreach.io",
    key_help="HeyReach → Integrations → HeyReach API: copy the workspace's API key. "
             "It acts on every sender account, campaign and conversation of that "
             "workspace; it never expires but can be deactivated there.",
)

CATEGORY = "Outreach"
PUBLISHER = "HeyReach"
LOGO_DOMAIN = "heyreach.io"

DESCRIPTION = (
    "LinkedIn outreach with HeyReach: read campaigns, their leads and statistics, "
    "lead lists and the unified LinkedIn inbox of your sender accounts; add leads to "
    "a campaign or a list, pause or resume a campaign, stop a lead, reply in a "
    "conversation, tag leads, and set webhooks that wake an agent on replies. Your "
    "own workspace API key."
)
