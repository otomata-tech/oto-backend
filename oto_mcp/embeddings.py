"""Embeddings de texte (lot 3, recherche sémantique V2) — client Mistral.

`mistral-embed` (dim 1024, ~0,10 €/M tokens) — même modèle que Memento v3, pour un
import sans re-embedding le jour du sunset. Deux surfaces :

- **sync `embed_texts`** : batch pour le WORKER d'indexation (`embed_worker`), appelé
  hors event loop (threadpool) → jamais de blocage de la boucle mono-loop ;
- **async `embed_query`** : un seul texte pour le CHEMIN REQUÊTE (`oto_search`), awaité
  par le handler async → pas de blocage non plus.

Gaté sur `MISTRAL_API_KEY` : sans clé, `enabled()` = False et tout le sémantique est
inerte (la recherche reste lexicale). Aucune dépendance oto-core (interne backend).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_URL = "https://api.mistral.ai/v1/embeddings"
MODEL = "mistral-embed"
DIM = 1024
# mistral-embed plafonne à ~8192 tokens/input → un corps de page long fait 400
# (all-or-nothing sur le batch). On tronque en CARACTÈRES (borne prudente ~4 ch/token) ;
# le début d'une page porte l'essentiel du sens pour la recherche. Empty → espace
# (l'API rejette une chaîne vide).
_MAX_CHARS = 16000


def _cap(text: str) -> str:
    t = (text or "").strip()
    return t[:_MAX_CHARS] if t else " "


def enabled() -> bool:
    return bool(os.environ.get("MISTRAL_API_KEY"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}",
            "Content-Type": "application/json"}


# Budget de tokens TOTAL par requête (mistral-embed : « Too many tokens overall,
# split into more batches » au-delà) — on découpe en sous-lots sous cette borne,
# estimée en caractères (~4 ch/token).
_REQ_CHAR_BUDGET = 16000


def _batches(texts: list[str]):
    cur: list[str] = []
    size = 0
    for t in texts:
        ct = _cap(t)
        if cur and size + len(ct) > _REQ_CHAR_BUDGET:
            yield cur
            cur, size = [], 0
        cur.append(ct)
        size += len(ct)
    if cur:
        yield cur


def embed_texts(texts: list[str], *, timeout: float = 30.0) -> list[list[float]]:
    """Batch SYNC (worker, threadpool). Ordre préservé, DÉCOUPÉ en sous-requêtes sous
    le budget de tokens (sinon 400 « too many tokens overall »). Lève sur échec
    réseau/API — le worker attrape et re-tente au prochain tour (la ligne reste dirty)."""
    if not texts or not enabled():
        return []
    out: list[list[float]] = []
    with httpx.Client(timeout=timeout) as c:
        for chunk in _batches(texts):
            r = c.post(_URL, headers=_headers(), json={"model": MODEL, "input": chunk})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
    return out


async def embed_query(text: str, *, timeout: float = 8.0) -> Optional[list[float]]:
    """Un texte, ASYNC (chemin requête). None si désactivé ou en échec — la recherche
    retombe alors sur le lexical seul (jamais d'erreur remontée à l'agent)."""
    text = (text or "").strip()
    if not text or not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(_URL, headers=_headers(), json={"model": MODEL, "input": [_cap(text)]})
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
    except Exception as e:  # noqa: BLE001 — dégradation gracieuse vers le lexical
        logger.warning("embed_query échec (fallback lexical) : %s", e)
        return None


def to_pg(vec: list[float]) -> str:
    """Sérialise un vecteur au littéral pgvector/halfvec (`[a,b,c]`)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
