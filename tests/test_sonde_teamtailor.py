"""La sonde de connexion Teamtailor — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /jobs` (déjà dans le client — `list_jobs`), `page_size=1`. Une liste
VIDE est un état normal (aucun poste créé), jamais un refus — seul le fait
que l'appel n'ait pas levé compte.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import teamtailor as T


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("teamtailor", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_jobs(self, page_size=None, page_number=None, status=None):
        self.appels.append(("list_jobs", page_size))
        if self._boom:
            raise self._boom
        return {"data": []}


def _brancher(monkeypatch, client):
    import oto.tools.teamtailor.client as tc
    monkeypatch.setattr(tc, "TeamtailorClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    T._verify(_fields("k"))
    assert cli.appels == [("list_jobs", 1)]


def test_une_cle_refusee_leve(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid token", service="teamtailor")))
    with pytest.raises(UpstreamHTTPError):
        T._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    T.register(FastMCP("t"))
    assert cv.supports("teamtailor")
    assert cv.probe_for("teamtailor") is T._verify
    assert cv.couverture("teamtailor") == cv.AUTH
