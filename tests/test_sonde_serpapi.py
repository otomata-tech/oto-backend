"""La sonde SerpApi — otomata-tech/oto#69. Couvre `auth+quota`.

`GET https://serpapi.com/account` — la doc SerpApi l'écrit noir sur blanc :
« Account API is free of charge, and using it will not be counted toward your
monthly quota. » Le solde vient au même appel (`total_searches_left`), donc
elle distingue une clé fausse (à remplacer) d'un compte à sec (à recharger).
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import serpapi as S


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("serpapi", secret)


class _FauxResponse:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _brancher(monkeypatch, body=None, status=200, boom=None):
    import requests

    def _get(url, params=None, timeout=None):
        if boom:
            raise boom
        return _FauxResponse(body, status)
    monkeypatch.setattr(requests, "get", _get)


def test_un_solde_disponible_est_rendu_avec_son_unite_et_son_instant(monkeypatch):
    _brancher(monkeypatch, {"account_id": "acc_1", "total_searches_left": 380})
    out = S._verify(_fields("k"))
    q = out["quota"]
    assert q["restant"] == 380
    assert q["unite"] == "recherches"
    assert q["mesure_a"], "ce chiffre vieillit dès qu'il est lu — l'instant compte"


def test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH(monkeypatch):
    _brancher(monkeypatch, {"account_id": "acc_1", "total_searches_left": 0})
    with pytest.raises(cv.QuotaEpuise) as e:
        S._verify(_fields("k"))
    assert cv.classer(e.value) == cv.NO_QUOTA
    assert "recharge" in str(e.value).lower()


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, {"error": "Invalid API key."}, status=401)
    with pytest.raises(Exception):
        S._verify(_fields("k"))


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    """Le cas qui rendrait la sonde décorative : 200, mais aucun compte désigné."""
    _brancher(monkeypatch, {})
    with pytest.raises(RuntimeError, match="sans identifier"):
        S._verify(_fields("k"))


def test_un_solde_ILLISIBLE_ne_fabrique_pas_de_quota(monkeypatch):
    _brancher(monkeypatch, {"account_id": "acc_1"})  # pas de total_searches_left
    assert S._verify(_fields("k")) == {}


def test_la_sonde_est_enregistree_avec_la_couverture_auth_quota():
    from fastmcp import FastMCP

    S.register(FastMCP("t"))
    assert cv.supports("serpapi")
    assert cv.probe_for("serpapi") is S._verify
    assert cv.couverture("serpapi") == cv.AUTH_QUOTA
