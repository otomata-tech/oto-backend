"""Client HTTP mince vers le service FOD — capacité « LOI » (codes consolidés LEGI).

Le stock LEGI (22 codes, versions historiques d'articles) est ingéré et indexé
par le service FOD (france-opendata-service#7). Ce module sert le geste central :
l'article d'un code **dans sa version en vigueur à une date donnée** (une décision
de 1992 cite l'art. 1128 CC → texte d'époque, pas la rédaction actuelle), plus la
timeline des versions et la recherche plein-texte.

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
            "FOD_REGLEMENT_API_TOKEN absents). Les codes consolidés sont servis "
            "par le service FOD dédié (france-opendata-service#7)."
        )
    if _client is None:
        _client = httpx.Client(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=_TIMEOUT,
        )
    return _client


def article(code: str, num: str, date: Optional[str] = None) -> dict[str, Any]:
    """Version d'un article en vigueur à `date` (défaut : aujourd'hui)."""
    params: dict[str, Any] = {"code": code, "num": num}
    if date:
        params["date"] = date
    r = _c().get("/api/loi/article", params=params)
    r.raise_for_status()
    return r.json()


def versions(code: str, num: str) -> dict[str, Any]:
    """Timeline complète d'un article (toutes ses versions)."""
    r = _c().get("/api/loi/versions", params={"code": code, "num": num})
    r.raise_for_status()
    return r.json()


def search(query: str, *, code: Optional[str] = None, en_vigueur: bool = True,
           limit: int = 20) -> dict[str, Any]:
    """Recherche FTS dans les articles des codes consolidés."""
    params: dict[str, Any] = {"query": query, "en_vigueur": en_vigueur, "limit": limit}
    if code:
        params["code"] = code
    r = _c().get("/api/loi/search", params=params)
    r.raise_for_status()
    return r.json()


def codes() -> dict[str, Any]:
    """Codes couverts (alias → LEGITEXT + libellé)."""
    r = _c().get("/api/loi/codes")
    r.raise_for_status()
    return r.json()
