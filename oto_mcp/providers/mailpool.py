"""Registry declaration for the `mailpool` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# mailpool: the cold-email infrastructure behind a sending tool (domains, DNS,
# mailboxes, warmup) — neighbour of `lemlist`, hence the Prospecting category.
#
# keyed `api_key`: one workspace key, sent in the `X-Api-Authorization` header.
# Strict BYOK (`byo_user` + `byo_org`): these are the customer's own sending
# domains and mailboxes. `mailpool_dns` is a helper module, not a tool module.
CONNECTOR = _c(
    "mailpool", ["mailpool"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Mailpool",
    help="cold-email domains, DNS, mailboxes, spam checks, warmup",
    href="https://www.mailpool.ai",
)

CATEGORY = "Outreach"
PUBLISHER = "Mailpool"
LOGO_DOMAIN = "mailpool.ai"

DESCRIPTION = (
    "The cold-email infrastructure behind your sending tool: domains and their "
    "DNS (SPF, DKIM, DMARC, tracking domain) audited and fixed, mailboxes, spam "
    "checks, warmup. Mailbox credentials are never exposed, and nothing billed "
    "(domains, mailboxes, slots) is bought. Workspace API key."
)
