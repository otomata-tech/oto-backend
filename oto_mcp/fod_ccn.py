"""Client HTTP mince vers le service FOD — capacité « CCN » (conventions collectives).

Le stock KALI DILA (~290k articles, ~1,4k conteneurs) est ingéré et indexé (FTS
`french` + filtre IDCC) **par le service FOD** (france-opendata-service#6) — JAMAIS
dans le chemin d'une requête MCP. Ce module sert la recherche d'articles, le texte
intégral et la résolution IDCC→convention depuis ce stock partagé.

Pas de fallback (même doctrine qu'ADR 0028) : service indisponible/mal configuré
⟹ erreur actionnable.

Config (env de process) : `FOD_REGLEMENT_BASE_URL` + `FOD_REGLEMENT_API_TOKEN` —
même instance que la capacité règlement (data.oto.zone, PG otomata-0).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

_BASE = os.environ.get("FOD_REGLEMENT_BASE_URL")
_TOKEN = os.environ.get("FOD_REGLEMENT_API_TOKEN")
# Recherche = FTS indexé GIN : timeout court.
_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

_client: Optional[httpx.Client] = None


def _c() -> httpx.Client:
    global _client
    if not _BASE or not _TOKEN:
        raise RuntimeError(
            "Service FOD non configuré (FOD_REGLEMENT_BASE_URL / "
            "FOD_REGLEMENT_API_TOKEN absents). Les conventions collectives sont "
            "servies par le service FOD dédié (france-opendata-service#6)."
        )
    if _client is None:
        _client = httpx.Client(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=_TIMEOUT,
        )
    return _client


def search(query: str, *, idcc: Optional[str] = None, en_vigueur: bool = True,
           limit: int = 20, sort: str = "relevance") -> dict[str, Any]:
    """Recherche FTS dans les articles de CCN, filtrable par IDCC."""
    params: dict[str, Any] = {"query": query, "en_vigueur": en_vigueur,
                              "limit": limit, "sort": sort}
    if idcc:
        params["idcc"] = idcc
    r = _c().get("/api/ccn/search", params=params)
    r.raise_for_status()
    return r.json()


def article(kali_id: str) -> dict[str, Any]:
    """Texte intégral d'un article (KALIARTI…) + contexte convention/avenant."""
    r = _c().get(f"/api/ccn/article/{kali_id}")
    r.raise_for_status()
    return r.json()


def conventions(*, idcc: Optional[str] = None, query: Optional[str] = None,
                limit: int = 20) -> dict[str, Any]:
    """Conventions (conteneurs KALI) par IDCC exact ou substring de titre."""
    params: dict[str, Any] = {"limit": limit}
    if idcc:
        params["idcc"] = idcc
    if query:
        params["query"] = query
    r = _c().get("/api/ccn/conventions", params=params)
    r.raise_for_status()
    return r.json()
