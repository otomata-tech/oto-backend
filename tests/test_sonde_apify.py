"""The Apify connection probe — covers `auth+quota`.

`GET /v2/users/me/limits` gives the monthly usage cap and what has been used in the
current cycle. The former probe listed actors: green on an account at its cap, whose
runs then all failed upstream.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import apify as A


def _fields(secret: str) -> dict:
    """Fields EXACTLY as the verify capability produces them (probe↔schema drift)."""
    return credentials_store.unpack_secret("apify", secret)


class _Reponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _brancher(monkeypatch, reponse):
    import requests
    vu = {}

    def _get(url, headers=None, timeout=None):
        vu.update(url=url, headers=headers)
        return reponse
    monkeypatch.setattr(requests, "get", _get)
    return vu


def _limites(plafond, consomme):
    return {"data": {"limits": {"maxMonthlyUsageUsd": plafond},
                     "current": {"monthlyUsageUsd": consomme}}}


def test_le_reste_du_mois_est_rendu_en_dollars(monkeypatch):
    vu = _brancher(monkeypatch, _Reponse(body=_limites(49, 12.345)))
    assert A._verify(_fields("tok")) == {
        "quota": {"restant": 36.66, "unite": "usd", "limite": 49}}
    assert vu["url"].endswith("/users/me/limits")
    assert vu["headers"] == {"Authorization": "Bearer tok"}


def test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH(monkeypatch):
    _brancher(monkeypatch, _Reponse(body=_limites(49, 49.2)))
    with pytest.raises(cv.QuotaEpuise) as e:
        A._verify(_fields("tok"))
    assert cv.classer(e.value) == cv.NO_QUOTA
    assert "cap is reached" in str(e.value)


@pytest.mark.parametrize("status", [401, 403])
def test_un_jeton_refuse_est_un_refus_d_AUTH(monkeypatch, status):
    _brancher(monkeypatch, _Reponse(status=status, body={}))
    with pytest.raises(cv.NonAutorise) as e:
        A._verify(_fields("tok"))
    assert cv.classer(e.value) == cv.UNAUTHORIZED


def test_un_usage_ILLISIBLE_echoue_plutot_que_d_inventer(monkeypatch):
    _brancher(monkeypatch, _Reponse(body={"data": {"limits": {}}}))
    with pytest.raises(RuntimeError, match="without a readable monthly usage"):
        A._verify(_fields("tok"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth_quota():
    from fastmcp import FastMCP

    A.register(FastMCP("t"))
    assert cv.supports("apify")
    assert cv.couverture("apify") == cv.AUTH_QUOTA
