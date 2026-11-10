"""Registry declaration of the `origami` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# origami: email + LinkedIn campaigns (origami.chat, API v2 Bearer `og_live_…`) —
# lead tables (CSV upload, upsert), campaigns written by the Origami agent,
# launch, pause/resume, stats, sequences. keyed api_key, **BYOK** (byo
# user/org): credits and sends are those of the org's account.
# ⚠️ First third-party integration whose WRITE SENDS outside the platform
# (launching = emails + LinkedIn messages to real people): every mutating tool
# is gated by `dry_run` (oto-wide convention), launch defaults to
# dry_run=True. Acceptability decision left to the maintainer (see tools/origami.py).
CONNECTOR = _c(
    "origami", ["origami"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key",
    label="Origami",
    help="email + LinkedIn campaigns: tables, campaigns, launch, statistics",
    href="https://origami.chat",
)

CATEGORY = "Outreach"
PUBLISHER = "Origami"
LOGO_DOMAIN = "origami.chat"

DESCRIPTION = (
    "Email AND LinkedIn campaigns driven by the Origami agent: lead tables "
    "(CSV import), campaign drafting and launch, pause and resume, "
    "statistics. Every real send goes through a trial mode (dry-run) enabled "
    "by default, before reaching real people."
)
