"""Registry declaration of the `urba` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "urba", ["urba"], secret_kind="none",
    label="Urban planning", help="PLU/GPU zoning, risks, QPV, EPFIF, commune socio-demographics (open data)",
)

DESCRIPTION = (
    "Regulatory urban planning as open data: PLU/GPU zoning and "
    "regulations, natural risks, clay soils, QPV and proximity, EPFIF, "
    "commune socio-demographics."
)
# French State open data (Géoportail de l'urbanisme, Géorisques, INSEE…), not an
# in-house connector: the publisher used to fall back to "Otomata" and the card was filed
# under "Other" — same family as `foncier` (2026-09-02).
CATEGORY = "Public data"
PUBLISHER = "French State (open data)"
LOGO_DOMAIN = "geoportail-urbanisme.gouv.fr"
