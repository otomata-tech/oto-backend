"""Le garde-fou réseau de `conftest.py` — otomata-tech/oto#69 (serper flaky,
05/09/2026). Bloqué par défaut, loopback libre, `@pytest.mark.reseau_reel`
avec raison lève le garde pour UN test.

Régression que ce fichier ferme : `test_serper_refus_local_473.py` monkeypatchait
une fonction qui n'existait pas au niveau module, `_Faux` n'était jamais exercé, et
un `except Exception: pass` avalait l'appel réseau réel qui suivait — invisible tant
que personne ne bloquait les sockets pour voir.
"""
from __future__ import annotations

import socket

import pytest

import conftest as _conftest


def test_une_connexion_sortante_reelle_est_bloquee():
    with pytest.raises(AssertionError, match="bloquée"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("93.184.216.34", 80))


def test_le_loopback_reste_libre():
    with pytest.raises(OSError):  # refus du système — PAS le garde
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("127.0.0.1", 1))


@pytest.mark.reseau_reel("smoke test du garde-fou lui-même")
def test_le_marqueur_avec_raison_leve_le_garde(monkeypatch):
    """Ne fait PAS de vraie connexion (un hôte réel peut filtrer plutôt que
    refuser, un test ne doit pas dépendre de son délai) — vérifie que le garde
    délègue bien au VRAI `connect()` plutôt que de lever, une fois marqué."""
    vu = {}
    monkeypatch.setattr(_conftest, "_SOCKET_CONNECT_ORIGINAL",
                        lambda self, addr: vu.setdefault("addr", addr))
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("93.184.216.34", 80))
    assert vu["addr"] == ("93.184.216.34", 80)
