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

    def search_batch(self, sirens: list[str], famille: Optional[str] = None,
                     chunk_size: Optional[int] = None) -> dict[str, Any]:
        return _post("/api/fr/bodacc/batch", {
            "sirens": sirens, "famille": famille, "chunk_size": chunk_size,
        })


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


# --- BOAMP (marchés publics) / ACCO (accords) : index PG possédé par FOD (B2b) ---
# Répliquent la surface des fonctions db.search_boamp/get_boamp/search_acco/... que
# tools/fr.py consommait sur la PG backend, désormais servies par FOD.

def search_boamp(query=None, descripteur=None, departement=None, date_from=None,
                 date_to=None, type_marche=None, limit=50, offset=0) -> dict[str, Any]:
    return _post("/api/fr/tenders/search", {
        "query": query, "descripteur": descripteur, "departement": departement,
        "date_from": date_from, "date_to": date_to, "type_marche": type_marche,
        "limit": limit, "offset": offset,
    })


def get_boamp(idweb: str) -> Optional[dict[str, Any]]:
    return _get(f"/api/fr/tenders/{idweb}")


def search_aides(insee=None, code_postal=None, effectif=None, nature=None,
                 echeance_avant=None, q=None, limit=50, offset=0) -> dict[str, Any]:
    import httpx

    try:
        return _post("/api/fr/aides/search", {
            "insee": insee, "code_postal": code_postal, "effectif": effectif,
            "nature": nature, "echeance_avant": echeance_avant, "q": q,
            "limit": limit, "offset": offset,
        })
    except httpx.HTTPStatusError as e:
        # 400 métier du service (commune/CP inconnu du référentiel) : re-lever le
        # detail actionnable, pas le message httpx générique.
        if e.response.status_code == 400:
            try:
                detail = e.response.json().get("detail", e.response.text)
            except Exception:
                detail = e.response.text
            raise ValueError(detail) from e
        raise


def get_aide(id_aid: str, raw: bool = False) -> Optional[dict[str, Any]]:
    return _get(f"/api/fr/aides/{id_aid}", {"raw": raw} if raw else None)


def search_acco(query=None, themes=None, nature=None, siren=None, siret=None,
                idcc=None, departement=None, date_from=None, date_to=None,
                latest_per_siret=False, sort_by="date", sort_dir="desc",
                limit=50, offset=0) -> dict[str, Any]:
    return _post("/api/fr/accords/search", {
        "query": query, "themes": themes, "nature": nature, "siren": siren,
        "siret": siret, "idcc": idcc, "departement": departement,
        "date_from": date_from, "date_to": date_to, "latest_per_siret": latest_per_siret,
        "sort_by": sort_by, "sort_dir": sort_dir, "limit": limit, "offset": offset,
    })


def get_acco(id_or_numero: str) -> Optional[dict[str, Any]]:
    return _get(f"/api/fr/accords/{id_or_numero}")


def acco_themes() -> list[dict[str, Any]]:
    return _get("/api/fr/accords/themes")


# --- INSEE SIRENE (keyé) : le backend résout la clé + track le quota, la passe à FOD ---
# par-appel (header X-Sirene-Key) ; FOD ne la stocke pas (ADR 0028/0037). BYO ou plateforme.

def insee_siret(siret: str, api_key: str) -> dict[str, Any]:
    return _get(f"/api/fr/insee/siret/{siret}", headers={"X-Sirene-Key": api_key})


def insee_headquarters(siren: str, api_key: str) -> Optional[dict[str, Any]]:
    return _get(f"/api/fr/insee/headquarters/{siren}", headers={"X-Sirene-Key": api_key})
