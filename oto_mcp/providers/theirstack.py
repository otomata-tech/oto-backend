"""Registry declaration of the `theirstack` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# theirstack: job postings by employer + technologies detected in the
# postings (technographics: ERP, CRM…). Two POST endpoints whose body is the
# vendor's filter DSL, passed through as is. keyed api_key (Bearer), byo by
# default — credit billing, per COMPANY record returned; platform key
# GRANT-ONLY since 26/08 (#405). Partial SME coverage: `data: []` is normal.
# Read-only.
CONNECTOR = _c(
    "theirstack", ["theirstack"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
    default_quota=0, platform_key_open=False,  # platform key on explicit grant (data bought on credit)
    secret_kind="api_key",
    label="TheirStack",
    help="job postings by employer + technologies used (ERP…)",
    href="https://theirstack.com",
)

CATEGORY = "Lead & company data"
PUBLISHER = "TheirStack"
LOGO_DOMAIN = "theirstack.com"

DESCRIPTION = (
    "Job postings published by a company, with the technologies "
    "detected in those postings (ERP, CRM…) — a technographic reading of its "
    "stack. Partial coverage on SMEs, read-only."
)
