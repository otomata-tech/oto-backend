"""Registry declaration of the `cognism` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# cognism: classic connector (kind="tools") on Cognism's Search API
# (developers.cognism.com). Synchronous REST client in oto-core
# (`oto.tools.cognism`), curated tools in `tools/cognism.py`. Standard key
# cascade (`resolve_api_key`) — BYO org covers the "one key for the whole
# org" need; "platform" mode GRANT-ONLY since 26/08 (#405, credits GTM —
# this doc said the opposite until then, for lack of an Otomata↔Cognism commercial agreement).
# search_contacts/search_accounts = preview only (`has*` flags, no real
# email/phone); redeem_contacts/redeem_accounts = full reveal
# (consumes credits); enrich_contact/enrich_account = lookup by
# identity (email/LinkedIn/name+company). Filter DSL (~150 fields)
# documented in the `cognism-filters` guide, not in the tool docstrings.
CONNECTOR = _c(
    "cognism", ["cognism"],
    auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought by credit)
    secret_kind="api_key",
    label="Cognism",
    help="B2B contact & company search, reveal, and identity enrichment",
    href="https://cognism.com",
)

CATEGORY = "Lead & company data"
PUBLISHER = "Cognism"
LOGO_DOMAIN = "cognism.com"

DESCRIPTION = (
    "B2B contact and company search at Cognism, with credit-based reveal "
    "(email, phone) and identity enrichment (email, "
    "LinkedIn, name + company). Searches stay in preview until "
    "reveal is explicitly requested."
)
