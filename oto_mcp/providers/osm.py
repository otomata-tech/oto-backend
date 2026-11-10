"""Registry declaration of the `osm` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

CONNECTOR = _c(
    "osm", ["osm"], secret_kind="none",
    label="OpenStreetMap", help="OSM points of interest by tag over an area (parking lots, facilities, shops) — exhaustive census via Overpass (open data)",
)

CATEGORY = "Public data"

# Third-party public data: the publisher used to fall back to the default "Otomata" while
# the card already carries the openstreetmap.org logo (fixed on 2026-09-02).
PUBLISHER = "OpenStreetMap"
LOGO_DOMAIN = "openstreetmap.org"

DESCRIPTION = (
    "OpenStreetMap points of interest over an area, filtered by tag "
    "(parking lots, facilities, shops…): an exhaustive census via "
    "Overpass, as open data."
)
