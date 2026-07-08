"""OpenStreetMap — points d'intérêt via Overpass (open data, sans clé).

Recense **tous** les objets OSM d'un tag sur une zone en un seul appel (pas de
plafond ni de pagination des API Maps). Servi par le service FOD (ADR 0028).

Connecteur open-data : pas de credential. Exposé si activé en DB (ADR 0010).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from .. import fod_osm

    overpass = fod_osm.overpass

    @mcp.tool()
    def osm_pois(selector: str, commune: Optional[str] = None,
                 departement: Optional[str] = None, bbox: Optional[str] = None,
                 limit: int = 500) -> dict:
        """Exhaustive OpenStreetMap POIs carrying a tag over an area (Overpass).

        Returns EVERY OSM object matching `selector` in the area, in ONE call —
        no result cap or pagination (unlike Maps APIs). Ideal for counting/listing
        infrastructure or businesses of a type for site analysis.

        `selector`: an OSM tag "key=value" (e.g. "shop=laundry", "amenity=parking",
        "amenity=school") or a bare key ("shop"). Area: give EXACTLY one of
        `commune` (5-digit INSEE code, e.g. "13055" = Marseille), `departement`
        (2-3 chars, e.g. "13"), or `bbox` = "south,west,north,east" lat/lon.

        Returns `count` + `pois` (each: osm_type, osm_id, name, lat, lon, postcode,
        tags). ⚠️ Coverage varies by tag: infrastructure (parking, schools,
        transport) is well mapped in OSM; consumer businesses less so (shops list
        on Google, not OSM) — for a full business census, cross with serper_maps_census.
        """
        box = None
        if bbox:
            parts = [p.strip() for p in bbox.split(",")]
            box = tuple(float(x) for x in parts) if len(parts) == 4 else None
        return overpass.pois(selector, commune=commune, departement=departement,
                             bbox=box, limit=limit)
