"""The in-app agent's turns in Slack: how many run AT THE SAME TIME (03/11/2026).

A Slack turn runs in the ACK's background task — a worker thread of the server's pool —
and HOLDS that thread for the whole turn (`slack_copilot._executer` waits on the loop).
Nothing bounded their number: a burst of mentions could take every thread of the pool,
and every route that runs off the loop (`run_in_threadpool`) would wait behind them. This
module counts the turns running, and refuses one too many:

- at most `OTO_SLACK_COPILOT_MAX_SIMULTANES` (default `MAX_DEFAUT`) in the process;
- at most `PAR_COMPTE` per account (`sub`) — one person does not take every place.

**Beyond: refused AT ONCE** (`Occupe`), never a wait and never a retry: the person is told
in Slack to ask again in a moment (`MESSAGE`). A turn that waited would still hold its
thread, and the queue would become the new load.

**A `threading.Lock`**: places are taken and given back from worker threads; the lock is
held only to count (no wait, no I/O).

⚠️ **The ceiling is PER PROCESS.** During a blue/green switch two colours run: the box's
effective ceiling doubles for the switch.
"""
from __future__ import annotations

import contextlib
import functools
import os
import threading
from typing import Iterator

ENV_MAX = "OTO_SLACK_COPILOT_MAX_SIMULTANES"
#: Eight turns hold eight threads of the pool (40 by default): the rest still serves.
MAX_DEFAUT = 8
#: The turns of ONE account at a time.
PAR_COMPTE = 1
#: What the person reads in Slack when there is no place.
MESSAGE = "I'm already busy — try again in a moment."


class ReglageInvalide(RuntimeError):
    """`OTO_SLACK_COPILOT_MAX_SIMULTANES` unreadable: raised at boot."""


class Occupe(RuntimeError):
    """No place for this turn (the process's, or this account's): nothing runs."""


def _lire() -> int:
    brut = os.environ.get(ENV_MAX)
    if brut is None or brut.strip() == "":
        return MAX_DEFAUT
    try:
        valeur = int(brut.strip())
    except ValueError:
        valeur = 0
    if valeur < 1:
        raise ReglageInvalide(f"{ENV_MAX}={brut!r} unreadable: an integer, at least 1.")
    return valeur


@functools.cache
def plafond() -> int:
    """The process's ceiling, read once. Tests start over with `cache_clear()`."""
    return _lire()


def verifier_configuration() -> None:
    """At boot (`server.main`): an unreadable setting refuses to start HERE."""
    plafond()


_verrou = threading.Lock()
_par_compte: dict[str, int] = {}
_total = 0


def en_cours() -> int:
    """The turns holding a place."""
    return _total


def _prendre(sub: str) -> None:
    global _total
    maximum = plafond()
    with _verrou:
        if _par_compte.get(sub, 0) >= PAR_COMPTE or _total >= maximum:
            raise Occupe(MESSAGE)
        _par_compte[sub] = _par_compte.get(sub, 0) + 1
        _total += 1


def _rendre(sub: str) -> None:
    global _total
    with _verrou:
        reste = _par_compte.get(sub, 0) - 1
        if reste > 0:
            _par_compte[sub] = reste
        else:
            _par_compte.pop(sub, None)
        _total -= 1


@contextlib.contextmanager
def place(sub: str) -> Iterator[None]:
    """A place for ONE turn of `sub`, given back on the way out whatever happens. No
    place: `Occupe`, at once, and nothing is taken."""
    _prendre(sub)
    try:
        yield
    finally:
        _rendre(sub)
