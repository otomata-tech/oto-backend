"""Registry declaration of the `waalaxy` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# waalaxy: LinkedIn prospecting automation. IMPORT-ONLY public API
# (4 endpoints: test, lists, active campaigns, adding prospects to a
# list ± campaign) — no reading/deleting of prospects, no inbox,
# no stats. keyed api_key (Bearer zpka_…, app → Settings → CRM Sync,
# Advanced/Business plans), byo-only: one key = ONE Waalaxy seat (= one
# LinkedIn account), a platform key would make no sense.
CONNECTOR = _c(
    "waalaxy", ["waalaxy"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Waalaxy",
    help="LinkedIn prospecting: push prospects into a Waalaxy list and "
         "campaign",
    href="https://app.waalaxy.com", credential_fields=(
        CredentialField("key", "API key (zpka_…)", secret=True,
                        help="Waalaxy → Settings → CRM Sync → Generate API key "
                             "(Advanced or Business plan)"),
    ),
)

CATEGORY = "Outreach"
PUBLISHER = "Waalaxy"
LOGO_DOMAIN = "waalaxy.com"

DESCRIPTION = (
    "Push prospects into a Waalaxy LinkedIn automation list and campaign "
    "— import only: no inbox reading or statistics, Waalaxy's public API "
    "stops at adding prospects."
)
