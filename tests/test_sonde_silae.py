"""La sonde de connexion Silae — otomata-tech/oto#69. Couvre `auth` SEUL.

`POST v1/Dossiers/ListeDossiers` (déjà dans le client — `list_dossiers`).
`SilaeClient.call` ne lève JAMAIS sur un refus HTTP (dict `{"error",
"status_code"}`, documenté dans `oto_mcp/tools/silae.py`) — la sonde DOIT lire
ce dict et lever elle-même. Credential à 3 champs (ADR 0011), round-trip par
le VRAI `pack_secret`/`unpack_secret`.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import silae as S


def _fields(client_id: str, client_secret: str, subscription_key: str) -> dict:
    secret = credentials_store.pack_secret("silae", {
        "client_id": client_id, "client_secret": client_secret,
        "subscription_key": subscription_key,
    })
    return credentials_store.unpack_secret("silae", secret)


class _FauxClient:
    def __init__(self, rendu):
        self._rendu = rendu
        self.appels = []

    def list_dossiers(self):
        self.appels.append("list_dossiers")
        return self._rendu


def _brancher(monkeypatch, client):
    import oto.tools.silae as pkg
    monkeypatch.setattr(pkg, "SilaeClient", lambda **kw: client)
    return client


def test_un_credential_valide_ne_leve_pas_liste_vide(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient([]))
    S._verify(_fields("id", "sec", "sub"))
    assert cli.appels == ["list_dossiers"]


def test_LE_POINT__un_dict_derreur_401_leve_non_autorise(monkeypatch):
    """Contre-épreuve : sans la lecture explicite du dict, un token/subscription
    key morte (401, rendue SANS exception par le client) passerait pour `ok:true`."""
    _brancher(monkeypatch, _FauxClient(
        {"error": "401", "status_code": 401, "details": "invalid token"}))
    with pytest.raises(cv.NonAutorise):
        S._verify(_fields("id", "sec", "sub"))


def test_une_erreur_non_auth_leve_sans_typer_non_autorise(monkeypatch):
    _brancher(monkeypatch, _FauxClient(
        {"error": "500", "status_code": 500, "details": "server error"}))
    with pytest.raises(RuntimeError) as e:
        S._verify(_fields("id", "sec", "sub"))
    assert not isinstance(e.value, cv.SondeRefusee)
    assert cv.classer(e.value) == cv.UNKNOWN


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    S.register(FastMCP("t"))
    assert cv.supports("silae")
    assert cv.probe_for("silae") is S._verify
    assert cv.couverture("silae") == cv.AUTH
