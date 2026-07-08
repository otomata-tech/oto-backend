"""Proxy HTTP vers la capacité OSM du service FOD (ADR 0028).

Objet proxy `overpass` répliquant la surface d'`OverpassClient` (france_opendata)
— méthode `pois` → GET /api/osm/pois. Le backend n'exécute plus Overpass
in-process ; il appelle FOD (pas de credential, données open data).
"""
from __future__ import annotations

from typing import Any, Optional

from .fod_http import get as _get


class _Overpass:
    def pois(self, selector: str, *, commune: Optional[str] = None,
             departement: Optional[str] = None,
             bbox: Optional[tuple] = None, limit: int = 500) -> dict[str, Any]:
        params: dict[str, Any] = {"selector": selector, "limit": limit}
        if commune is not None:
            params["commune"] = commune
        if departement is not None:
            params["departement"] = departement
        if bbox is not None:
            params["bbox"] = ",".join(str(x) for x in bbox)
        return _get("/api/osm/pois", params)


overpass = _Overpass()
