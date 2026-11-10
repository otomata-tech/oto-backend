"""Registry declaration of the `topograph` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# topograph: KYB — normalized data + documents from 100+ European public
# registries through a single REST API. byo by default (pay-per-request; shareable
# org key), keyed api_key (x-api-key header resolved client-side); platform
# key GRANT-ONLY since 26/08 (#405). Outside the base kit: opt-in.
CONNECTOR = _c(
    "topograph", ["topograph"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought on credit)
    secret_kind="api_key",
    label="Topograph",
    # The acronym "KYB" opened the help text without being spelled out (2026-09-02).
    help="official company records and documents for European companies, from "
         "public registries — vet a customer or a supplier (KYB)",
    href="https://www.topograph.co",
)

CATEGORY = "Lead & company data"
# The displayed publisher fell back to the default "Otomata" while the card
# carries the topograph.co logo — two contradicting claims in the same place.
PUBLISHER = "Topograph"
LOGO_DOMAIN = "topograph.co"

DESCRIPTION = (
    "Company identity verification (KYB): official company records and documents "
    "for European companies, aggregated from over 100 public registries, "
    "through a single API."
)
