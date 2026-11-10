"""Registry declaration of the `onet` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# onet: the US Department of Labor's occupation reference (O*NET
# Web Services v2) — search an occupation, its description, its tasks, its
# job titles. keyed api_key (`X-API-Key` header), byo-only: the key is
# free but personal (developer sign-up, terms of use accepted
# by its holder), no platform key is set.
CONNECTOR = _c(
    "onet", ["onet"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="O*NET",
    help="US occupation reference: find an O*NET-SOC code, "
         "read an occupation's description, its tasks and its job titles",
    href="https://services.onetcenter.org", credential_fields=(
        CredentialField("key", "API key", secret=True,
                        help="services.onetcenter.org → Sign up (gratuit) → "
                             "My Account → API keys"),
    ),
)

CATEGORY = "Public data"
PUBLISHER = "O*NET (U.S. Department of Labor)"
LOGO_DOMAIN = "onetcenter.org"

DESCRIPTION = (
    "The US Department of Labor's occupation reference: "
    "search by keyword or by code, then for each occupation its description, "
    "its tasks and the job titles actually encountered. The "
    "O*NET-SOC code found here gives the SOC code of wage statistics."
)
