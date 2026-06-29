"""Client HTTP mince vers le service FOD — capacité « règlement » (ADR 0028).

Les règlements écrits de PLU/PLUi sont des PDF lourds (souvent >50 Mo, >1000 pages,
plusieurs zones). Le download + parse pdftotext + indexation est fait **une fois par
`idurba`** par le service FOD dédié (instance « règlement », PG otomata-0 + texte en
S3) — JAMAIS dans le chemin d'une requête MCP. Ce module sert les **extraits**
ciblés (par zone / mot-clé) depuis ce cache partagé.

Cache-miss = `{cached: False}` : le service n'ingère pas à la volée (l'ingestion
vit dans un batch côté service). On NE refait PAS le parsing en local : le backend
ne doit plus porter le PDF ni la dépendance poppler (cf. retrait d'`urba_reglement`
local). Pas de fallback (ADR 0028) : service indisponible/mal configuré ⟹ erreur
actionnable.

Config (env de process) : `FOD_REGLEMENT_BASE_URL` (ex. https://data.oto.zone) +
`FOD_REGLEMENT_API_TOKEN` (clé S2S). Instance distincte du stock SIRENE (`fod_client`,
box fod-0) — même service, capacités séparées.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

_BASE = os.environ.get("FOD_REGLEMENT_BASE_URL")
_TOKEN = os.environ.get("FOD_REGLEMENT_API_TOKEN")
# Lecture d'extraits = pas de scan (lecture S3 + recherche en mémoire) : timeout court.
_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

_client: Optional[httpx.Client] = None


def _c() -> httpx.Client:
    global _client
    if not _BASE or not _TOKEN:
        raise RuntimeError(
            "Service FOD règlement non configuré (FOD_REGLEMENT_BASE_URL / "
            "FOD_REGLEMENT_API_TOKEN absents). Les règlements PLU/PLUi sont servis "
            "par le service FOD dédié (ADR 0028)."
        )
    if _client is None:
        _client = httpx.Client(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=_TIMEOUT,
        )
    return _client


def extraits(idurba: str, *, zone: Optional[str] = None, query: Optional[str] = None,
             max_extraits: int = 8, context_lignes: int = 30) -> dict[str, Any]:
    """Extraits du règlement écrit d'un PLU/PLUi (cache partagé par `idurba`).

    Renvoie `{idurba, cached, ...}` ; `cached=False` si le règlement n'est pas
    encore ingéré dans le service.
    """
    params: dict[str, Any] = {
        "idurba": idurba,
        "max_extraits": max_extraits,
        "context_lignes": context_lignes,
    }
    if zone:
        params["zone"] = zone
    if query:
        params["query"] = query
    r = _c().get("/api/reglement", params=params)
    r.raise_for_status()
    return r.json()
