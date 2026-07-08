"""Client HTTP mince vers le service FOD — capacité « frenchtech » (ADR 0028, B3).

Écosystème French Tech (annuaire/membres/événements/appels/financements/FTC) servi
par le service FOD. Objet proxy `ft` répliquant la surface de `FrenchTechClient`.
Pas de fallback (ADR 0028).
"""
from __future__ import annotations

from typing import Any, Optional

from .fod_http import get as _get, post as _post


class _FrenchTech:
    def search_annuaire(self, query: Optional[str] = None, secteur: Optional[str] = None,
                        ville: Optional[str] = None, type_annuaire: Optional[str] = None,
                        all_results: bool = False, per_page: int = 100) -> dict[str, Any]:
        return _post("/api/frenchtech/annuaire/search", {
            "query": query, "secteur": secteur, "ville": ville,
            "type_annuaire": type_annuaire, "all_results": all_results, "per_page": per_page,
        })

    def get_annuaire(self, slug: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/frenchtech/annuaire/{slug}")

    def list_membres(self, query: Optional[str] = None, all_results: bool = False) -> dict[str, Any]:
        return _get("/api/frenchtech/membres", {"query": query, "all_results": all_results})

    def list_evenements(self, query: Optional[str] = None, all_results: bool = False) -> dict[str, Any]:
        return _get("/api/frenchtech/evenements", {"query": query, "all_results": all_results})

    def list_appels(self, query: Optional[str] = None, all_results: bool = False) -> dict[str, Any]:
        return _get("/api/frenchtech/appels", {"query": query, "all_results": all_results})

    def list_financements(self, query: Optional[str] = None, all_results: bool = False) -> dict[str, Any]:
        return _get("/api/frenchtech/financements", {"query": query, "all_results": all_results})

    def ftc_scenarios(self) -> dict[str, Any]:
        return _get("/api/frenchtech/ftc_scenarios")


ft = _FrenchTech()
