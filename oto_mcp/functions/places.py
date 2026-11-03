"""Les places du bac à sable : combien d'exécutions tournent EN MÊME TEMPS (03/11/2026).

Chaque exécution est bornée (`isolement` : 1 Go, un cœur, 64 tâches, sa durée) ; rien ne
bornait leur NOMBRE. Tout compte qui lit une fonction peut la lancer, tout membre peut
éprouver du code : N exécutions simultanées pesaient N × 1 Go sur la box. Ce module tient
le compte des exécutions en cours, et en refuse une de trop :

- au plus `OTO_FUNCTIONS_MAX_SIMULTANEES` (défaut `MAX_DEFAUT`) dans le processus ;
- au plus `PAR_COMPTE` par compte (`sub`) — un compte ne prend pas toutes les places.

**Au-delà : refus IMMÉDIAT**, `BacASableOccupe` (429 `sandbox_busy` côté capacité, avec
`Retry-After`), jamais une attente : une requête qui attend une place tient sa
connexion, et la file deviendrait la nouvelle charge.

**Le point commun** : l'aller simple (`executor.executer`) et le pont des recettes-code
(`pont.executer_avec_pont`) prennent leur place avant de lancer le processus et la
rendent après qu'il est mort (`finally` : exception, délai, annulation compris).

**Un `threading.Lock`, pas un `asyncio.Semaphore`** : l'aller simple joue dans un fil
(`run_in_threadpool`), le pont dans la boucle. Un sémaphore asyncio n'est ni sûr entre
fils ni utilisable hors de sa boucle ; un verrou de fil l'est des deux côtés. Il n'est
jamais tenu au-delà d'un compte (prendre ou rendre une place, sans attente ni E/S) : la
boucle ne l'attend pas.

⚠️ **Le plafond est PAR PROCESSUS.** Pendant une bascule bleu/vert, deux couleurs
tournent : le plafond effectif de la box double le temps de la bascule.
"""
from __future__ import annotations

import contextlib
import functools
import os
import threading
from typing import Iterator, Optional

ENV_MAX = "OTO_FUNCTIONS_MAX_SIMULTANEES"
#: 4 × `isolement.MEMOIRE_MO` (1 Go) = 4 Go au pire, sous les ~6 Go que la box laisse
#: libres au service : il reste de quoi servir le reste pendant que quatre exécutions
#: tiennent leur plafond mémoire.
MAX_DEFAUT = 4
#: Les exécutions simultanées d'UN compte.
PAR_COMPTE = 1
#: Ce que le refus conseille d'attendre avant de réessayer (`Retry-After`).
ATTENTE_S = 10


class ReglageInvalide(RuntimeError):
    """`OTO_FUNCTIONS_MAX_SIMULTANEES` illisible : levé au démarrage."""


class BacASableOccupe(RuntimeError):
    """Plus de place dans le bac à sable (le processus, ou ce compte) : rien n'est lancé."""

    def __init__(self, message: str, retry_after: int = ATTENTE_S):
        super().__init__(message)
        self.retry_after = retry_after


def _lire() -> int:
    brut = os.environ.get("OTO_FUNCTIONS_MAX_SIMULTANEES")
    if brut is None or brut.strip() == "":
        return MAX_DEFAUT
    try:
        valeur = int(brut.strip())
    except ValueError:
        valeur = 0
    if valeur < 1:
        raise ReglageInvalide(f"{ENV_MAX}={brut!r} illisible : un entier, au moins 1.")
    return valeur


@functools.cache
def plafond() -> int:
    """Le plafond du processus, lu une fois. Les bancs repartent par `cache_clear()`."""
    return _lire()


def verifier_configuration() -> None:
    """Au démarrage (`server.main`) : un réglage illisible refuse de démarrer ICI."""
    plafond()


_verrou = threading.Lock()
_par_compte: dict[Optional[str], int] = {}
_total = 0


def en_cours() -> int:
    """Les exécutions qui tiennent une place."""
    return _total


def _prendre(sub: Optional[str]) -> None:
    global _total
    maximum = plafond()
    with _verrou:
        if _par_compte.get(sub, 0) >= PAR_COMPTE:
            raise BacASableOccupe(
                f"sandbox_busy: this account already has {PAR_COMPTE} execution running "
                f"in the sandbox; retry in {ATTENTE_S} s, once it has finished.")
        if _total >= maximum:
            raise BacASableOccupe(
                f"sandbox_busy: the sandbox is running its maximum of {maximum} "
                f"simultaneous executions; retry in {ATTENTE_S} s.")
        _par_compte[sub] = _par_compte.get(sub, 0) + 1
        _total += 1


def _rendre(sub: Optional[str]) -> None:
    global _total
    with _verrou:
        reste = _par_compte.get(sub, 0) - 1
        if reste > 0:
            _par_compte[sub] = reste
        else:
            _par_compte.pop(sub, None)
        _total -= 1


@contextlib.contextmanager
def place(sub: Optional[str]) -> Iterator[None]:
    """Une place pour UNE exécution de `sub`, rendue en sortant quoi qu'il arrive. Pas
    de place : `BacASableOccupe`, tout de suite, et rien n'est pris."""
    _prendre(sub)
    try:
        yield
    finally:
        _rendre(sub)
