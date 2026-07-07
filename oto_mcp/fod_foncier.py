"""Clients HTTP minces vers le service FOD — capacité « foncier » (ADR 0028).

Le stock de données de site (géocodage BAN, cadastre IGN, bâti BDTOPO, productible
PVGIS, permis Sit@del, conso Enedis, valorisation DVF+, DPE ADEME) est servi par le
service FOD dédié (box `fod-0`) — le backend ne les exécute plus in-process.

Ce module expose des **objets proxy** (`ban`, `cadastre`, `bdtopo`, `pvgis`,
`enedis`, `dvf`, `dpe`, `sitadel`) qui **répliquent la surface des clients
`france_opendata`** consommés par `tools/foncier.py` (mêmes noms/signatures/retours)
→ le tool ne change que la SOURCE de ses clients, ses corps restent identiques.

Pas de fallback in-process (ADR 0028) : FOD indisponible ⟹ erreur actionnable.
"""
from __future__ import annotations

from typing import Any, Optional

from .fod_http import get as _get, post as _post


class _Ban:
    def search(self, adresse: str, limit: int = 5, postcode: Optional[str] = None,
               citycode: Optional[str] = None) -> list[dict[str, Any]]:
        return _post("/api/foncier/ban/search",
                     {"adresse": adresse, "limit": limit, "postcode": postcode, "citycode": citycode})

    def reverse(self, lat: float, lon: float) -> Optional[dict[str, Any]]:
        return _get("/api/foncier/ban/reverse", {"lat": lat, "lon": lon})


class _Cadastre:
    def parcelle_at(self, lat: float, lon: float) -> Optional[dict[str, Any]]:
        return _get("/api/foncier/cadastre/parcelle", {"lat": lat, "lon": lon})


class _BdTopo:
    def bati_parcelle(self, geometry: dict, contenance_m2: Optional[float] = None) -> dict[str, Any]:
        return _post("/api/foncier/bdtopo/bati", {"geometry": geometry, "contenance_m2": contenance_m2})


class _Pvgis:
    def productible(self, lat: float, lon: float, kwc: float) -> Optional[dict[str, Any]]:
        return _get("/api/foncier/pvgis/productible", {"lat": lat, "lon": lon, "kwc": kwc})


class _Sitadel:
    def search(self, kind: str, communes: Optional[str] = None, dept: Optional[str] = None,
               an_min: Optional[int] = None, an_max: Optional[int] = None,
               page: int = 1, page_size: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/sitadel/search",
                     {"kind": kind, "communes": communes, "dept": dept,
                      "an_min": an_min, "an_max": an_max, "page": page, "page_size": page_size})


class _Enedis:
    def consommation_par_adresse(self, annee: str, dept: str, secteur: Optional[str] = None,
                                 min_mwh: Optional[float] = None, max_mwh: Optional[float] = None,
                                 limit: int = 200) -> dict[str, Any]:
        return _post("/api/foncier/enedis/conso",
                     {"annee": annee, "dept": dept, "secteur": secteur,
                      "min_mwh": min_mwh, "max_mwh": max_mwh, "limit": limit})


class _Dvf:
    def stats(self, code_commune: str, type_local: Optional[str] = None, years: int = 3) -> dict[str, Any]:
        return _post("/api/foncier/dvf/stats",
                     {"code_commune": code_commune, "type_local": type_local, "years": years})

    def comparables(self, code_commune: str, type_local: Optional[str] = None,
                    surface_min: Optional[float] = None, surface_max: Optional[float] = None,
                    years: int = 2, limit: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/dvf/comparables",
                     {"code_commune": code_commune, "type_local": type_local,
                      "surface_min": surface_min, "surface_max": surface_max,
                      "years": years, "limit": limit})

    def comparables_by_address(self, adresse: str, radius_m: int = 500, type_local: Optional[str] = None,
                               surface_min: Optional[float] = None, surface_max: Optional[float] = None,
                               years: int = 3, limit: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/dvf/comparables_by_address",
                     {"adresse": adresse, "radius_m": radius_m, "type_local": type_local,
                      "surface_min": surface_min, "surface_max": surface_max,
                      "years": years, "limit": limit})


class _Dpe:
    def by_address(self, adresse: str, radius_m: int = 200, type_batiment: Optional[str] = None,
                   etiquette: Optional[str] = None, surface_min: Optional[float] = None,
                   surface_max: Optional[float] = None, limit: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/dpe/by_address",
                     {"adresse": adresse, "radius_m": radius_m, "type_batiment": type_batiment,
                      "etiquette": etiquette, "surface_min": surface_min, "surface_max": surface_max,
                      "limit": limit})

    def stats(self, code_commune: str, type_batiment: Optional[str] = None) -> dict[str, Any]:
        return _post("/api/foncier/dpe/stats",
                     {"code_commune": code_commune, "type_batiment": type_batiment})


ban = _Ban()
cadastre = _Cadastre()
bdtopo = _BdTopo()
pvgis = _Pvgis()
sitadel = _Sitadel()
enedis = _Enedis()
dvf = _Dvf()
dpe = _Dpe()
