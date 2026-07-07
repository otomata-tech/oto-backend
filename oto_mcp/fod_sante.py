"""Clients HTTP minces vers le service FOD — capacité « sante » (ADR 0028, B3).

FINESS (annuaire) + HAS ESSMS (évaluations qualité, DuckDB sur parquet distant —
workload lourd) servis par le service FOD. Objets proxy répliquant la surface des
clients `france_opendata`. Pas de fallback (ADR 0028).
"""
from __future__ import annotations

from typing import Any, Optional

from .fod_http import get as _get, post as _post


class _Finess:
    def search(self, q: str, departement: Optional[str] = None,
               categorie: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        return _get("/api/sante/finess/search",
                    {"q": q, "departement": departement, "categorie": categorie, "limit": limit})

    def by_code(self, code: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/sante/finess/{code}")


class _HasEssms:
    def search(self, region_libelle: Optional[str] = None, departement_code: Optional[str] = None,
               secteur: Optional[str] = None, type_structure: Optional[str] = None,
               statut_juridique: Optional[str] = None, annee_min: Optional[int] = None,
               annee_max: Optional[int] = None, limit: int = 50) -> dict[str, Any]:
        return _post("/api/sante/essms/search", {
            "region_libelle": region_libelle, "departement_code": departement_code,
            "secteur": secteur, "type_structure": type_structure,
            "statut_juridique": statut_juridique, "annee_min": annee_min,
            "annee_max": annee_max, "limit": limit,
        })

    def dimensions(self) -> dict[str, Any]:
        return _get("/api/sante/essms/dimensions")


finess = _Finess()
has = _HasEssms()
