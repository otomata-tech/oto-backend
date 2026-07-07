"""Cœur HTTP partagé des clients FOD (ADR 0028).

Le backend n'exécute plus les workloads data in-process : il appelle le service
FOD dédié (box `fod-0`) en HTTP. Ce module porte la **plomberie commune** —
client httpx singleton, auth Bearer S2S, gestion d'erreurs, retry borné sur la
saturation (503) — réutilisée par tous les clients de domaine (`fod_client` pour
SIRENE, `fod_foncier`, puis fr/urba/sante/frenchtech).

Pas de fallback in-process (ADR 0028) : FOD indisponible/mal configuré ⟹ erreur
actionnable, jamais un calcul local silencieux.

Config (env de process) : `FOD_BASE_URL` (ex. http://<ip-fod>:8000) + `FOD_API_TOKEN`.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any, Optional

import httpx

_BASE = os.environ.get("FOD_BASE_URL")
_TOKEN = os.environ.get("FOD_API_TOKEN")
# Lecture longue : le timeout DUR de FOD (scan SIRENE) est ~90 s, on laisse FOD
# répondre/erreur avant de couper côté client (connexion courte, lecture large).
_TIMEOUT = httpx.Timeout(connect=5.0, read=100.0, write=10.0, pool=5.0)

# Back-pressure de saturation (503) : le service borne la concurrence du scan et
# REJETTE en non-bloquant dès le plafond « en vol ». L'attente est DÉLÉGUÉE à
# l'appelant → on absorbe les rafales transitoires par un retry borné à backoff
# jitteré. 504 (scan trop long) n'est PAS retryé : le répéter regaspille un slot.
_RETRY_ATTEMPTS = int(os.environ.get("FOD_RETRY_ATTEMPTS", "3"))
_RETRY_BACKOFF_S = float(os.environ.get("FOD_RETRY_BACKOFF_S", "0.5"))

_client: Optional[httpx.Client] = None


def _c() -> httpx.Client:
    global _client
    if not _BASE or not _TOKEN:
        raise RuntimeError(
            "Service FOD non configuré (FOD_BASE_URL / FOD_API_TOKEN absents). "
            "Les données france-opendata sont servies par le service FOD dédié (ADR 0028)."
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


def _request(method: str, path: str, *, params: Optional[dict] = None,
             json_body: Optional[dict] = None) -> Any:
    """Appel HTTP avec retry borné sur 503 (saturation transitoire du scan)."""
    r: Optional[httpx.Response] = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        r = _c().request(method, path, params=params, json=json_body)
        if r.status_code != 503 or attempt == _RETRY_ATTEMPTS:
            break
        # backoff exponentiel + jitter : laisse un slot de scan se libérer.
        time.sleep(_RETRY_BACKOFF_S * (2 ** attempt) + random.uniform(0, _RETRY_BACKOFF_S))
    _raise_for(r)
    return r.json()


def get(path: str, params: Optional[dict] = None) -> Any:
    return _request("GET", path, params=params)


def post(path: str, body: Optional[dict] = None) -> Any:
    return _request("POST", path, json_body=body)
