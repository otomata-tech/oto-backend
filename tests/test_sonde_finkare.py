"""La sonde de connexion Finkare — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET invoices` (déjà dans le client — `list_invoices`). Une liste VIDE est un
état normal (aucune créance), jamais un refus — seul le fait que l'appel
n'ait pas levé compte.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import finkare as F


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("finkare", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_invoices(self, status=None, page=None, **kw):
        self.appels.append("list_invoices")
        if self._boom:
            raise self._boom
        return {"data": []}


def _brancher(monkeypatch, client):
    import oto.tools.finkare as pkg
    monkeypatch.setattr(pkg, "FinkareClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    F._verify(_fields("fk_live_k"))
    assert cli.appels == ["list_invoices"]


def test_une_cle_refusee_leve(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid key", service="finkare")))
    with pytest.raises(UpstreamHTTPError):
        F._verify(_fields("fk_live_k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    F.register(FastMCP("t"))
    assert cv.supports("finkare")
    assert cv.probe_for("finkare") is F._verify
    assert cv.couverture("finkare") == cv.AUTH
