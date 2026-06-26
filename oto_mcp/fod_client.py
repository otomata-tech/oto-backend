"""Client HTTP mince vers le service FOD (ADR 0028, barreau 4).

Le backend n'exécute plus le scan SIRENE in-process : il appelle le service FOD
dédié (box `fod-0`) qui porte le parquet partitionné par dept + les durcissements
(sémaphore de concurrence + timeout). Ce module **réplique la surface de
`france_opendata.sirene_stock`** (mêmes noms/signatures/retours) → les appelants
(`tools/fr_stock`, `api_routes_sirene`) ne changent que leur import.

Pas de fallback in-process (ADR : un scan parquet ne doit plus tourner sur la box
backend) : FOD indisponible/mal configuré ⟹ erreur actionnable, jamais un scan
local silencieux.

Config (env de process) : `FOD_BASE_URL` (ex. http://<ip-fod>:8000) + `FOD_API_TOKEN`.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Optional

import httpx

_BASE = os.environ.get("FOD_BASE_URL")
_TOKEN = os.environ.get("FOD_API_TOKEN")
# Lecture longue : le timeout DUR de FOD est ~90 s, on laisse FOD répondre/erreur
# avant de couper côté client (connexion courte, lecture large).
_TIMEOUT = httpx.Timeout(connect=5.0, read=100.0, write=10.0, pool=5.0)

_client: Optional[httpx.Client] = None


def _c() -> httpx.Client:
    global _client
    if not _BASE or not _TOKEN:
        raise RuntimeError(
            "Service FOD non configuré (FOD_BASE_URL / FOD_API_TOKEN absents). "
            "Le stock SIRENE est servi par le service FOD dédié (ADR 0028)."
        )
    if _client is None:
        _client = httpx.Client(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=_TIMEOUT,
        )
    return _client


def _detail(r: httpx.Response) -> str:
    try:
        return r.json().get("detail", r.text)
    except Exception:
        return r.text


def _raise_for(r: httpx.Response) -> None:
    if r.status_code == 503:
        raise RuntimeError(f"FOD saturé — réessayez ({_detail(r)})")
    if r.status_code == 504:
        raise RuntimeError(f"FOD: requête trop longue ({_detail(r)})")
    r.raise_for_status()


def _get(path: str, params: Optional[dict] = None) -> Any:
    r = _c().get(path, params=params)
    _raise_for(r)
    return r.json()


def _post(path: str, body: dict) -> Any:
    r = _c().post(path, json=body)
    _raise_for(r)
    return r.json()


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
    return _post("/v1/search", body)["items"]


def lookup_siege(siren: str) -> Optional[dict[str, Any]]:
    return _get(f"/v1/siege/{siren}")


def lookup_sieges(sirens: Iterable[str]) -> dict[str, dict[str, Any]]:
    return _post("/v1/sieges", {"sirens": list(sirens)})


def headquarters_addresses(sirens: Iterable[str]) -> dict[str, dict[str, Any]]:
    return _post("/v1/enrich", {"sirens": list(sirens)})


def list_establishments(siren: str, active_only: bool = True) -> list[dict[str, Any]]:
    return _get(f"/v1/etablissements/{siren}", {"active_only": active_only})


def lookup_siret(siret: str) -> Optional[dict[str, Any]]:
    return _get(f"/v1/siret/{siret}")


def parquet_info() -> dict[str, Any]:
    return _get("/health").get("parquet", {})
