"""Registry declaration of the `productlane` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# productlane: customer feedback (threads, contacts, companies), public roadmap
# and help center. Neighbor of `linear` — same "Product & data" category — and it is not
# just a thematic kinship: **Productlane's roadmap IS backed by Linear**.
# Projects and issues are created in Linear first, then mirrored here. An org
# that has both connectors therefore sees the same objects through two doors, and that
# is normal.
#
# ⚠️ **v2** API (`/api/v2`, Bearer). A v1 key does not work: v1 is a separate
# API, which shuts down on 2026-11-20.
#
# BYO org first (customer feedback belongs to the organization, not to one
# person), but `byo_user` stays open: the key is created per member on the
# Productlane side, and an org that is starting out often sets up its PM's before
# turning it into a team key. No platform mode — these are the org's
# customer conversations.
CONNECTOR = _c(
    "productlane", ["productlane"], auth_modes={"byo_user", "byo_org"},
    keyed=True, secret_kind="api_key",
    label="Productlane",
    help="customer feedback (threads, contacts, companies), Linear-backed "
         "roadmap, changelogs and help center",
    href="https://productlane.com",
)

CATEGORY = "Product & data"
PUBLISHER = "Productlane"
LOGO_DOMAIN = "productlane.com"

DESCRIPTION = (
    "Customer feedback raised in Productlane: discussion threads, "
    "contacts, companies, plus the public roadmap and the help center. The "
    "roadmap is backed by Linear — the same projects and issues are found "
    "on both sides."
)
