"""La sonde de connexion Zapier — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /exposed/` (déjà dans le client — `list_actions`). Une liste VIDE est un
état normal (aucune action exposée), jamais un refus — seul le fait que
l'appel n'ait pas levé compte.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import zapier as Z


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("zapier", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_actions(self):
        self.appels.append("list_actions")
        if self._boom:
            raise self._boom
        return {"results": []}


def _brancher(monkeypatch, client):
    import oto.tools.zapier as pkg
    monkeypatch.setattr(pkg, "ZapierClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    Z._verify(_fields("k"))
    assert cli.appels == ["list_actions"]


def test_une_cle_refusee_leve(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid key", service="zapier")))
    with pytest.raises(UpstreamHTTPError):
        Z._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    Z.register(FastMCP("t"))
    assert cv.supports("zapier")
    assert cv.probe_for("zapier") is Z._verify
    assert cv.couverture("zapier") == cv.AUTH
