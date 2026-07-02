"""Client HTTP mince vers le service FOD — capacité « JURIS » (jurisprudence).

Six fonds DILA (Cass publiés/inédits, cours d'appel, CE/CAA/TA, Conseil
constitutionnel, CNIL) ingérés et indexés par le service FOD
(france-opendata-service#8). Recherche unifiée triée pertinence × autorité
jurisprudentielle, texte intégral par identifiant.

Pas de fallback : service indisponible/mal configuré ⟹ erreur actionnable.
Config : `FOD_REGLEMENT_BASE_URL` + `FOD_REGLEMENT_API_TOKEN` (même instance).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

_BASE = os.environ.get("FOD_REGLEMENT_BASE_URL")
_TOKEN = os.environ.get("FOD_REGLEMENT_API_TOKEN")
_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

_client: Optional[httpx.Client] = None


def _c() -> httpx.Client:
    global _client
    if not _BASE or not _TOKEN:
        raise RuntimeError(
            "Service FOD non configuré (FOD_REGLEMENT_BASE_URL / "
            "FOD_REGLEMENT_API_TOKEN absents). La jurisprudence est servie "
            "par le service FOD dédié (france-opendata-service#8)."
        )
    if _client is None:
        _client = httpx.Client(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=_TIMEOUT,
        )
    return _client


def search(query: str, *, fond: Optional[str] = None,
           juridiction: Optional[str] = None,
           date_min: Optional[str] = None, date_max: Optional[str] = None,
           limit: int = 20, expand: bool = True) -> dict[str, Any]:
    """Recherche FTS unifiée, tri pertinence × autorité (+ expansion thésaurus)."""
    params: dict[str, Any] = {"query": query, "limit": limit, "expand": expand}
    for k, v in (("fond", fond), ("juridiction", juridiction),
                 ("date_min", date_min), ("date_max", date_max)):
        if v:
            params[k] = v
    r = _c().get("/api/juris/search", params=params)
    r.raise_for_status()
    return r.json()


def decision(decision_id: str) -> dict[str, Any]:
    """Texte intégral d'une décision (JURITEXT/CETATEXT/CONSTEXT/CNILTEXT…)."""
    r = _c().get(f"/api/juris/decision/{decision_id}")
    r.raise_for_status()
    return r.json()
