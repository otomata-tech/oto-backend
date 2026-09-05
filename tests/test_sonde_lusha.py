"""La sonde de connexion Lusha — otomata-tech/oto#69. Couvre `auth+quota`.

`GET /account/usage`. La doc Lusha l'écrit noir sur blanc : cet endpoint n'a
AUCUN coût pour vérifier son solde (« no charge for checking your balance »).
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import lusha as L


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("lusha", secret)


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom
        self.appels = []

    def _request(self, method, path, **kw):
        self.appels.append((method, path))
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.lusha.client as lc
    monkeypatch.setattr(lc, "LushaClient", lambda **kw: client)
    return client


def test_un_solde_disponible_est_rendu(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient({"remaining": 100, "total": 500}))
    out = L._verify(_fields("k"))
    assert out["quota"]["restant"] == 100
    assert cli.appels == [("GET", "/account/usage")]


def test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH(monkeypatch):
    _brancher(monkeypatch, _FauxClient({"remaining": 0, "total": 500}))
    with pytest.raises(cv.QuotaEpuise) as e:
        L._verify(_fields("k"))
    assert cv.classer(e.value) == cv.NO_QUOTA


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: unauthorized")))
    with pytest.raises(RuntimeError, match="401"):
        L._verify(_fields("k"))


def test_un_solde_ILLISIBLE_echoue_plutot_que_d_inventer(monkeypatch):
    _brancher(monkeypatch, _FauxClient({"foo": "bar"}))
    with pytest.raises(RuntimeError, match="sans solde"):
        L._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth_quota():
    from fastmcp import FastMCP

    L.register(FastMCP("t"))
    assert cv.supports("lusha")
    assert cv.couverture("lusha") == cv.AUTH_QUOTA
