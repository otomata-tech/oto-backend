"""Registry declaration for the `spott` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# spott: ATS **and** CRM for recruitment firms (agencies/staffing) — the
# candidate AND the client company in the same product, hence a wider scope
# than other ATSs (clients, client contacts, placements/fees).
# keyed api_key (x-api-key header), byo-only: each firm sets ITS own key.
CONNECTOR = _c(
    "spott", ["spott"], auth_modes={"byo_user", "byo_org"}, keyed=True,
    secret_kind="api_key", label="Spott",
    help="ATS and CRM for recruitment firms — candidates, jobs and "
         "applications, plus clients and placements",
    href="https://spott.io",
)

CATEGORY = "HR & recruiting"
PUBLISHER = "Spott"
LOGO_DOMAIN = "spott.io"

DESCRIPTION = (
    "The ATS AND the CRM of a recruitment firm: candidates, jobs and "
    "applications, plus client companies and invoiced placements "
    "— the candidate and the client in the same product."
)
