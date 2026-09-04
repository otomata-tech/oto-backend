"""Cœur HTTP partagé des clients FOD (ADR 0028).

Le backend n'exécute plus les workloads data in-process : il appelle le service
FOD dédié (box `fod-0`) en HTTP. Ce module porte la **plomberie commune** —
client httpx singleton, auth Bearer S2S, gestion d'erreurs, retry borné sur la
saturation (503) — réutilisée par tous les clients de domaine (`client` pour
SIRENE, `foncier`, puis `fr`/`urba`/`sante`/`frenchtech`).

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
    # noqa: SILENT — corps non-JSON : le texte brut EST le détail rendu
    except Exception:
        return r.text


def _raise_for(r: httpx.Response) -> None:
    if r.status_code == 503:
        raise RuntimeError(f"FOD saturé — réessayez ({_detail(r)})")
    if r.status_code == 504:
        raise RuntimeError(f"FOD: requête trop longue ({_detail(r)})")
    if r.status_code == 429:
        # Le quota amont a tenu malgré les reprises. Ce refus est RÉESSAYABLE et
        # ne dit rien sur la donnée demandée : sans cette phrase, il remontait en
        # `HTTPStatusError: … 429 …` dans la ligne de résultat, où il se lisait
        # comme une propriété de l'entreprise (otomata-tech/oto#44). FOD sait déjà
        # le dire — il suffit de ne pas perdre son détail.
        raise RuntimeError(
            f"Quota de l'API amont atteint, malgré {_RETRY_ATTEMPTS} reprises — "
            f"RÉESSAYABLE plus tard ou par lots plus petits ; ce n'est pas un fait "
            f"sur la donnée demandée ({_detail(r)})")
    r.raise_for_status()


# Attente maximale honorée sur un `Retry-After` amont. Au-delà, réessayer n'a plus
# de sens dans le temps d'un appel d'agent : mieux vaut rendre un refus nommé et
# réessayable que faire patienter sans le dire.
_RETRY_AFTER_MAX_S = float(os.environ.get("FOD_RETRY_AFTER_MAX_S", "10"))


def _retry_after_s(r: "httpx.Response") -> Optional[float]:
    """Le délai que l'amont DEMANDE, s'il le dit et qu'il tient dans nos bornes.

    FOD relaie l'en-tête `Retry-After` du fournisseur avec ses 429. Ne pas le lire,
    c'est jeter la seule information qui dit quand réessayer utilement — et se
    rabattre sur un backoff aveugle, ou sur rien du tout.
    """
    brut = (r.headers.get("Retry-After") or "").strip()
    if not brut:
        return None
    try:
        secondes = float(brut)
    except ValueError:
        return None                       # forme date HTTP : non gérée, on backoff
    if secondes <= 0 or secondes > _RETRY_AFTER_MAX_S:
        return None
    return secondes


def _request(method: str, path: str, *, params: Optional[dict] = None,
             json_body: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
    """Appel HTTP avec retry borné sur 503 (saturation du scan) et 429 (quota amont).

    Le 429 n'était PAS repris : il partait en exception, et le `Retry-After` que FOD
    relaie n'était jamais lu (otomata-tech/oto#44). Or ce 429-là est très souvent un
    artefact de NOTRE propre pression — un lot de 50 SIREN déclenche le quota par IP
    du fournisseur, et les mêmes SIREN redemandés plus calmement passent. Le refuser
    sans réessayer transforme une saturation passagère en fait sur la donnée.

    On honore le délai demandé quand l'amont le donne, sinon backoff exponentiel.
    """
    r: Optional[httpx.Response] = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        r = _c().request(method, path, params=params, json=json_body, headers=headers)
        if r.status_code not in (503, 429) or attempt == _RETRY_ATTEMPTS:
            break
        # Le délai DEMANDÉ prime sur le nôtre : l'amont sait quand son quota
        # se rouvre, nous ne faisons que deviner.
        demande = _retry_after_s(r) if r.status_code == 429 else None
        time.sleep(demande if demande is not None
                   # backoff exponentiel + jitter : laisse un slot se libérer.
                   else _RETRY_BACKOFF_S * (2 ** attempt) + random.uniform(0, _RETRY_BACKOFF_S))
    _raise_for(r)
    return r.json()


def get(path: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
    return _request("GET", path, params=params, headers=headers)


def post(path: str, body: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
    return _request("POST", path, json_body=body, headers=headers)
