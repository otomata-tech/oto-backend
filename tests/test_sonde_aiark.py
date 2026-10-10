"""La sonde de connexion AI Ark — couvre `auth+quota` (otomata-tech/oto#144).

`verify_key()` (oto-core) appelle `GET v1/payments/credits` et rend
`{"valid": True, "credits": <int>}`. La sonde jetait ce solde : sur un compte à
zéro elle restait verte, et un préflight partait travailler pour prendre des 402.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import aiark as A


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit (drift sonde↔schéma)."""
    return credentials_store.unpack_secret("aiark", secret)


class _FauxClient:
    def __init__(self, credits=None, boom=None):
        self._credits, self._boom = credits, boom
        self.appels = 0

    def verify_key(self):
        self.appels += 1
        if self._boom:
            raise self._boom
        return {"valid": True, "credits": self._credits}


def _brancher(monkeypatch, client):
    import oto.tools.aiark.client as ac
    monkeypatch.setattr(ac, "AiArkClient", lambda **kw: client)
    return client


def test_un_solde_disponible_est_rendu(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient(credits=250))
    assert A._verify(_fields("k")) == {"quota": {"restant": 250, "unite": "credits"}}
    assert cli.appels == 1


def test_un_solde_DECIMAL_est_un_solde(monkeypatch):
    """AI Ark answers a decimal balance since October 2026 (39519.7): the int-only
    check read a healthy account as "unreadable"."""
    _brancher(monkeypatch, _FauxClient(credits=39519.7))
    assert A._verify(_fields("k")) == {"quota": {"restant": 39519.7, "unite": "credits"}}


def test_un_booleen_n_est_pas_un_solde(monkeypatch):
    _brancher(monkeypatch, _FauxClient(credits=True))
    with pytest.raises(RuntimeError, match="without a readable credit balance"):
        A._verify(_fields("k"))


def test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH(monkeypatch):
    _brancher(monkeypatch, _FauxClient(credits=0))
    with pytest.raises(cv.QuotaEpuise) as e:
        A._verify(_fields("k"))
    assert cv.classer(e.value) == cv.NO_QUOTA
    assert "Top up" in str(e.value)


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: unauthorized")))
    with pytest.raises(RuntimeError, match="401"):
        A._verify(_fields("k"))


def test_un_solde_ILLISIBLE_echoue_plutot_que_d_inventer(monkeypatch):
    _brancher(monkeypatch, _FauxClient(credits=None))
    with pytest.raises(RuntimeError, match="without a readable credit balance"):
        A._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth_quota():
    from fastmcp import FastMCP

    A.register(FastMCP("t"))
    assert cv.supports("aiark")
    assert cv.couverture("aiark") == cv.AUTH_QUOTA
