"""La sonde de connexion Lever — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /v1/users` (déjà dans le client — `list_users`), `limit=1`. Une liste
VIDE est un état normal (aucun recruteur créé), jamais un refus — seul le
fait que l'appel n'ait pas levé compte.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import lever as L


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("lever", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_users(self, limit=None, offset=None):
        self.appels.append(("list_users", limit, offset))
        if self._boom:
            raise self._boom
        return []


def _brancher(monkeypatch, client):
    import oto.tools.lever.client as lc
    monkeypatch.setattr(lc, "LeverClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    L._verify(_fields("k"))
    assert cli.appels == [("list_users", 1, None)]


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid key")))
    with pytest.raises(RuntimeError, match="401"):
        L._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    L.register(FastMCP("t"))
    assert cv.supports("lever")
    assert cv.probe_for("lever") is L._verify
    assert cv.couverture("lever") == cv.AUTH
