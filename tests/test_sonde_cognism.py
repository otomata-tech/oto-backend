"""La sonde de connexion Cognism — otomata-tech/oto#69. Couvre `auth` SEUL.

`verify_key()` — déjà dans le client oto-core, écrit pour exactement cet
usage : un appel entitlement sans effet de bord, qui lève sur clé invalide.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import cognism as C


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("cognism", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def verify_key(self):
        self.appels.append("verify_key")
        if self._boom:
            raise self._boom
        return {"valid": True}


def _brancher(monkeypatch, client):
    import oto.tools.cognism.client as cc
    monkeypatch.setattr(cc, "CognismClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    C._verify(_fields("k"))
    assert cli.appels == ["verify_key"]


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid key")))
    with pytest.raises(RuntimeError, match="401"):
        C._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    C.register(FastMCP("t"))
    assert cv.supports("cognism")
    assert cv.probe_for("cognism") is C._verify
    assert cv.couverture("cognism") == cv.AUTH
