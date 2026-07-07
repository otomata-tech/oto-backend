"""Clients HTTP minces vers le service FOD — capacité « fr » (ADR 0028, B2a).

Les données entreprise open-data (identité/dirigeants via Recherche Entreprises,
événements BODACC, bilans INPI/BCE, index Egapro) sont servies par le service FOD
dédié (box `fod-0`) — le backend ne les exécute plus in-process. L'INPI est
particulièrement concerné : c'est du DuckDB sur parquet, désormais hors event-loop.

Objets proxy (`entreprises`, `bodacc`, `inpi`, `egapro`) répliquant la surface des
clients `france_opendata` consommés par `tools/fr.py` (mêmes noms/signatures) → le
tool ne change que la SOURCE de ces clients.

Hors périmètre : INSEE SIRENE (keyé, reste backend) et BOAMP/ACCO (index PG, B2b).
Pas de fallback in-process (ADR 0028).
"""
from __future__ import annotations

from typing import Any, Optional

from .fod_http import get as _get, post as _post


class _Entreprises:
    def search(self, query: Optional[str] = None, naf: Optional[list] = None,
               departement: Optional[str] = None, code_postal: Optional[str] = None,
               commune: Optional[str] = None, employees: Optional[list] = None,
               categorie_entreprise: Optional[str] = None, ca_min: Optional[int] = None,
               ca_max: Optional[int] = None, idcc: Optional[list] = None,
               page: int = 1, per_page: int = 25) -> dict[str, Any]:
        return _post("/api/fr/entreprises/search", {
            "query": query, "naf": naf, "departement": departement,
            "code_postal": code_postal, "commune": commune, "employees": employees,
            "categorie_entreprise": categorie_entreprise, "ca_min": ca_min, "ca_max": ca_max,
            "idcc": idcc, "page": page, "per_page": per_page,
        })

    def get_by_siren(self, siren: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/fr/entreprises/{siren}")

    def get_directors(self, siren: str) -> list[dict[str, Any]]:
        return _get(f"/api/fr/entreprises/{siren}/directors")


class _Bodacc:
    def search_by_siren(self, siren: str, famille: Optional[str] = None,
                        limit: int = 20) -> dict[str, Any]:
        return _get(f"/api/fr/bodacc/{siren}", {"famille": famille, "limit": limit})


class _Inpi:
    def list_exercises(self, siren: str) -> list[dict[str, Any]]:
        return _get(f"/api/fr/inpi/{siren}/exercises")

    def get_bilan(self, siren: str, date_cloture: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/fr/inpi/{siren}/bilan", {"date_cloture": date_cloture})


class _Egapro:
    def declaration(self, siren: str, year: int) -> Optional[dict[str, Any]]:
        return _get(f"/api/fr/egapro/{siren}/declaration", {"year": year})

    def latest_declaration(self, siren: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/fr/egapro/{siren}/latest")


entreprises = _Entreprises()
bodacc = _Bodacc()
inpi = _Inpi()
egapro = _Egapro()
