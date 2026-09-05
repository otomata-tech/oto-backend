"""La sonde de connexion GoCardless — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /creditors` (déjà dans le client — `list_creditors`). `GoCardlessClient.fetch`
ne lève JAMAIS sur un refus HTTP (dict `{"error", "status_code"}`) — la sonde DOIT
lire ce dict et lever elle-même, sinon un token mort répondrait `ok:true`.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import gocardless as G


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("gocardless", secret)


class _FauxClient:
    def __init__(self, rendu):
        self._rendu = rendu
        self.appels = []

    def fetch(self, endpoint, params=None, retries=3):
        self.appels.append(endpoint)
        return self._rendu


def _brancher(monkeypatch, client):
    import oto.tools.gocardless as pkg
    monkeypatch.setattr(pkg, "GoCardlessClient", lambda **kw: client)
    return client


def test_un_credential_valide_ne_leve_pas(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient({"creditors": [{"id": "CR123"}]}))
    G._verify(_fields("k"))
    assert cli.appels == ["creditors"]


def test_LE_POINT__un_dict_derreur_401_leve_non_autorise(monkeypatch):
    """Contre-épreuve : sans la lecture explicite du dict, un token mort (401,
    rendu SANS exception par le client) passerait pour `ok:true`."""
    _brancher(monkeypatch, _FauxClient(
        {"error": "401", "status_code": 401, "details": "Invalid token"}))
    with pytest.raises(cv.NonAutorise):
        G._verify(_fields("k"))


def test_une_erreur_non_auth_leve_sans_typer_non_autorise(monkeypatch):
    _brancher(monkeypatch, _FauxClient(
        {"error": "500", "status_code": 500, "details": "server error"}))
    with pytest.raises(RuntimeError) as e:
        G._verify(_fields("k"))
    assert not isinstance(e.value, cv.SondeRefusee)
    assert cv.classer(e.value) == cv.UNKNOWN


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    G.register(FastMCP("t"))
    assert cv.supports("gocardless")
    assert cv.probe_for("gocardless") is G._verify
    assert cv.couverture("gocardless") == cv.AUTH
