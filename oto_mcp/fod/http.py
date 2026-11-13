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
import re
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


# Adresses privées (RFC 1918 / loopback) : le service FOD vit sur une IP interne, et
# `FOD_BASE_URL` la porte. Un message d'erreur est SERVI à l'utilisateur — il ne nomme
# jamais une adresse de notre réseau. Mesuré le 2026-09-09 : un 502 de FOD remontait en
# « Server error '502 Bad Gateway' for url 'http://<ip-interne>:8000/api/foncier/…' ».
_ADRESSE_PRIVEE_RE = re.compile(
    r"(?:https?://)?"
    r"(?:10\.\d{1,3}|127\.\d{1,3}|192\.168|169\.254|"
    r"172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?::\d+)?",
    re.I,
)
_SERVICE = "le service de données interne"


def _public(texte: str) -> str:
    """Le texte tel qu'on accepte de le SERVIR : sans adresse de notre réseau.

    Ce qui remonte d'ici finit dans une ligne de résultat d'agent, donc sous les yeux
    d'un utilisateur et dans les journaux. Une IP interne n'y apprend rien à personne
    d'utile, et en apprend à qui n'a rien à y faire.
    """
    net = _ADRESSE_PRIVEE_RE.sub(_SERVICE, texte or "")
    if _BASE:
        net = net.replace(_BASE, _SERVICE).replace(_BASE.rstrip("/"), _SERVICE)
    return net


def _detail(r: httpx.Response) -> str:
    try:
        brut = r.json().get("detail", r.text)
    # noqa: SILENT — corps non-JSON : le texte brut EST le détail rendu
    except Exception:
        brut = r.text
    return _public(str(brut))[:400]


def _chemin(r: httpx.Response) -> str:
    """Le CHEMIN appelé, jamais l'URL absolue — celle-ci porte l'hôte interne."""
    try:
        return r.request.url.path
    # noqa: SILENT — réponse construite sans requête (tests) : le chemin est inconnu
    except Exception:
        return "?"


class FodError(RuntimeError):
    """Un refus du service FOD, qui PORTE son statut HTTP et son détail.

    Une `RuntimeError` nue n'en portait aucun : la taxonomie des erreurs
    (`error_taxonomy.classify`) n'y lisait aucun statut amont et la servait en
    « Internal server error. » — un 502 « source amont indisponible », un 400
    « commune inconnue » et une vraie panne se lisaient pareil (#1454).
    `status_code` est l'attribut que la taxonomie lit ; `detail` est le texte du
    service, déjà purgé de toute adresse interne.
    """

    def __init__(self, status_code: int, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


def _raise_for(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    detail = _detail(r)
    raise FodError(r.status_code, _message(r, detail), detail)


def _message(r: httpx.Response, detail: str) -> str:
    if r.status_code == 503:
        return f"FOD saturé — réessayez ({detail})"
    if r.status_code == 504:
        return f"FOD: requête trop longue ({detail})"
    if r.status_code == 429:
        # Le quota amont a tenu malgré les reprises. Ce refus est RÉESSAYABLE et
        # ne dit rien sur la donnée demandée : sans cette phrase, il remontait en
        # `HTTPStatusError: … 429 …` dans la ligne de résultat, où il se lisait
        # comme une propriété de l'entreprise (otomata-tech/oto#44). FOD sait déjà
        # le dire — il suffit de ne pas perdre son détail.
        return (
            f"Quota de l'API amont atteint, malgré {_RETRY_ATTEMPTS} reprises — "
            f"RÉESSAYABLE plus tard ou par lots plus petits ; ce n'est pas un fait "
            f"sur la donnée demandée ({detail})")
    if r.status_code == 502:
        # 502 = FOD a bien répondu, mais l'API PUBLIQUE qu'il interroge ne lui a rien
        # rendu. Ce n'est ni notre panne ni un fait sur la donnée : le dire évite de
        # lire « pas de mutation ici » là où il faut lire « source amont muette ».
        return (
            f"Source amont indisponible ({_chemin(r)}) : l'API publique interrogée "
            f"n'a pas répondu. Ce n'est pas un fait sur la donnée demandée — "
            f"RÉESSAYABLE plus tard. ({detail})")
    # JAMAIS `r.raise_for_status()` ici : httpx met l'URL ABSOLUE dans son message,
    # donc l'hôte et le port de FOD — une adresse de notre réseau, servie telle
    # quelle à l'utilisateur (mesuré le 2026-09-09 sur un 500 de `sitadel/search`).
    return f"FOD: {r.status_code} sur {_chemin(r)} ({detail})"


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
             json_body: Optional[dict] = None, headers: Optional[dict] = None,
             timeout: Optional[float] = None) -> Any:
    """Appel HTTP avec retry borné sur 503 (saturation du scan) et 429 (quota amont).

    Le 429 n'était PAS repris : il partait en exception, et le `Retry-After` que FOD
    relaie n'était jamais lu (otomata-tech/oto#44). Or ce 429-là est très souvent un
    artefact de NOTRE propre pression — un lot de 50 SIREN déclenche le quota par IP
    du fournisseur, et les mêmes SIREN redemandés plus calmement passent. Le refuser
    sans réessayer transforme une saturation passagère en fait sur la donnée.

    On honore le délai demandé quand l'amont le donne, sinon backoff exponentiel.
    """
    r: Optional[httpx.Response] = None
    kw: dict[str, Any] = {}
    if timeout is not None:
        # Attente BORNÉE pour les lectures dont l'amont est connu pour tomber : sans
        # elle, l'appel s'échoue au timeout de la passerelle (~60 s mesurées) et rend
        # une panne lente et muette là où un refus nommé en quelques secondes suffit.
        kw["timeout"] = httpx.Timeout(connect=5.0, read=timeout, write=10.0, pool=5.0)
    for attempt in range(_RETRY_ATTEMPTS + 1):
        try:
            r = _c().request(method, path, params=params, json=json_body,
                             headers=headers, **kw)
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Source amont indisponible ({path}) : pas de réponse en "
                f"{timeout or _TIMEOUT.read:.0f} s. L'API publique interrogée ne "
                f"répond pas — ce n'est pas un fait sur la donnée demandée, "
                f"RÉESSAYABLE plus tard."
            ) from exc
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


def get(path: str, params: Optional[dict] = None, headers: Optional[dict] = None,
        timeout: Optional[float] = None) -> Any:
    return _request("GET", path, params=params, headers=headers, timeout=timeout)


def post(path: str, body: Optional[dict] = None, headers: Optional[dict] = None,
         timeout: Optional[float] = None) -> Any:
    return _request("POST", path, json_body=body, headers=headers, timeout=timeout)
