"""Registry declaration of the `greenhouse` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# Recruiting connectors (Applicant Tracking Systems). byo keyed api_key
# (everyone sets their own Harvest/API key, user > org cascade), outside the bundle (opt-in,
# activatable per org/admin). Inert until activated in DB (deny-by-default,
# like hubspot/apollo). Recruitee = 2-field credential (token + company id)
# → resolve_credential_fields, not keyed.
CONNECTOR = _c(
    "greenhouse", ["greenhouse"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Greenhouse",
    help="ATS — candidates, jobs, applications, notes (Harvest API)",
    href="https://www.greenhouse.io",
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Greenhouse"
LOGO_DOMAIN = "greenhouse.io"

DESCRIPTION = (
    "Recruiting tracked in Greenhouse (ATS): candidates, job postings, "
    "applications and notes, via the Harvest API. Outside the base, to be activated per org."
)
