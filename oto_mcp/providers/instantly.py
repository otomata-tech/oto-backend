"""Registry declaration of the `instantly` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _decrit

# instantly: cold email outreach (API V2) — campaigns and their analytics, leads
# and lead lists, the Unibox (replies), sending accounts, the block list and
# webhooks. A neighbour of `lemlist` by trade. Served by the GENERIC READER of its
# description (`tools/decrits.py`): no `tools/instantly.py`, the library's engine
# runs the description (auth `bearer`). Label, namespace, modes and the credential
# field are read in the description: `byo_user` and `byo_org`, no platform key (the
# campaigns, leads and mailboxes are the customer's own). The key is kept under
# `key`, resolved by `access.resolve_api_key`.
#
# A key carries SCOPES chosen at its creation: a read-only key (`all:read`) serves
# `instantly` and every `instantly_act` op answers 403 with the missing scope
# named (the description's own refusal). What emails people, replies or posts to an
# outside address is `send`; moving leads and deleting a webhook `destroy` (derived
# into `tools/effets.py`, never written there).
CONNECTOR = _decrit(
    "instantly",
    help="cold email outreach: campaigns and analytics, leads, replies, webhooks",
    href="https://instantly.ai",
    key_help="Instantly → Settings → Integrations → API keys → Create API key (API V2). "
             "Scopes: all:all to read and act, all:read to read only (actions are then "
             "refused). The key reaches one workspace.",
)

CATEGORY = "Outreach"
PUBLISHER = "Instantly"
LOGO_DOMAIN = "instantly.ai"

DESCRIPTION = (
    "Cold email outreach with Instantly: read campaigns and their analytics, leads, "
    "lead lists and the Unibox replies; add leads to a campaign or a list, update a "
    "lead or its interest status, start or pause a campaign, reply to an email, block "
    "an address, and register webhooks that wake agents on replies. A workspace API "
    "key with the scopes to use (all:read reads only)."
)
