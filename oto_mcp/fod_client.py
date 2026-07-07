"""Client HTTP mince vers le service FOD — capacité SIRENE stock (ADR 0028, barreau 4).

Le backend n'exécute plus le scan SIRENE in-process : il appelle le service FOD
dédié (box `fod-0`) qui porte le parquet partitionné par dept + les durcissements
(sémaphore de concurrence + timeout). Ce module **réplique la surface de
`france_opendata.sirene_stock`** (mêmes noms/signatures/retours) → les appelants
(`tools/fr_stock`, `api_routes_sirene`) ne changent que leur import.

La plomberie HTTP (client, auth, retry, erreurs) vit dans `fod_http` (partagée avec
les autres clients FOD). Pas de fallback in-process (ADR 0028).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .fod_http import get as _get, post as _post


# --- Surface identique à france_opendata.sirene_stock ----------------------

def search(
    naf: Optional[str] = None,
    code_commune: Optional[str] = None,
    code_postal: Optional[str] = None,
    departement: Optional[str] = None,
    denomination: Optional[str] = None,
    enseigne: Optional[str] = None,
    active_only: bool = True,
    sieges_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    tranche_effectifs: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    body = {
        "naf": naf,
        "code_commune": code_commune,
        "code_postal": code_postal,
        "departement": departement,
        "denomination": denomination,
        "enseigne": enseigne,
        "active_only": active_only,
        "sieges_only": sieges_only,
        "tranche_effectifs": list(tranche_effectifs) if tranche_effectifs else None,
        "limit": limit,
        "offset": offset,
    }
    return _post("/api/sirene/search", body)["items"]


def lookup_siege(siren: str) -> Optional[dict[str, Any]]:
    return _get(f"/api/sirene/siege/{siren}")


def lookup_sieges(sirens: Iterable[str]) -> dict[str, dict[str, Any]]:
    return _post("/api/sirene/sieges", {"sirens": list(sirens)})


def headquarters_addresses(sirens: Iterable[str]) -> dict[str, dict[str, Any]]:
    return _post("/api/sirene/enrich", {"sirens": list(sirens)})


def list_establishments(siren: str, active_only: bool = True) -> list[dict[str, Any]]:
    return _get(f"/api/sirene/etablissements/{siren}", {"active_only": active_only})


def lookup_siret(siret: str) -> Optional[dict[str, Any]]:
    return _get(f"/api/sirene/siret/{siret}")


def parquet_info() -> dict[str, Any]:
    return _get("/api/sirene/info")
