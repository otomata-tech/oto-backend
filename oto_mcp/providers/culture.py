"""Registry declaration of the `culture` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "culture", ["culture"], secret_kind="none",
    # The connector covers ONLY live performance: "Culture" promised
    # heritage, museums, audiovisual (2026-09-02).
    label="Spectacle vivant",
    help="companies holding a live-performance entrepreneur licence — "
         "open data from the Ministère de la Culture",
)

CATEGORY = "Public data"
PUBLISHER = "Ministère de la Culture"
DESCRIPTION = (
    "Live-performance companies, from the Ministère de la Culture "
    "open data: multi-criteria search, detailed records, "
    "sector statistics and export."
)
LOGO_DOMAIN = "culture.gouv.fr"
