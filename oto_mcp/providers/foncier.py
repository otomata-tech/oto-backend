"""Registry declaration of the `foncier` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# foncier / sante: declared open-data connectors (ADR 0010). Inert until
# activated in DB (connector_activation) — register_all gates on it,
# so absent from the initial seed → OFF by default (deny-by-default).
CONNECTOR = _c(
    "foncier", ["foncier"], secret_kind="none",
    # "Foncier" alone misrepresented half the box: geocoding, isochrones,
    # permits and electricity consumption are not land/property (2026-09-02).
    # The same gap replays on the usage side: electricity consumption serves a
    # commercial targeting (the big consumers of a sector) as much as PV
    # prospecting, and nobody looks for that under "Foncier". Hence the explicit
    # mention below — it's the description, not the label, that makes a
    # connector findable.
    label="Foncier & territoire",
    help="addresses and parcels, buildings, price per m² (DVF), DPE, site electricity consumption "
         "(distribution AND transmission), risks and ICPE, permits, isochrones — open data",
)

CATEGORY = "Public data"
PUBLISHER = "French State (open data)"
DESCRIPTION = (
    "French sites in open data: BAN geocoding, cadastral parcels, "
    "buildings, DVF transactions (price per m², comparables by address), risks and "
    "ICPE, DPE, solar yield — and the annual electricity consumption "
    "of sites on BOTH grid tiers, distribution (Enedis) and "
    "transmission (RTE), the tertiary DPE and declared GHG assessments — "
    "everything needed to build a list of large energy consumers and qualify it."
)
LOGO_DOMAIN = "data.gouv.fr"
