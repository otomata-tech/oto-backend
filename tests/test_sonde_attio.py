"""La sonde de connexion Attio — otomata-tech/oto#69. Couvre `auth` + DROITS.

`GET /v2/self` (introspection RFC 7662, le SEUL endpoint Attio hors de la forme
`{"data": ...}` du reste de l'API). Une clé morte n'y lève PAS — Attio répond
200 avec `{"active": false}` — donc le premier cas à couvrir est celui qui
rendrait la sonde décorative si on ne le vérifiait pas explicitement.
"""
from __future__ import annotations

import json

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import attio as A


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("attio", secret)


class _Reponse:
    """Même forme que `tests/test_attio_upstream_error.py` : ce que
    `requests.request` rend réellement, seule pièce doublée."""

    def __init__(self, status: int, corps: dict):
        self.status_code = status
        self.ok = status < 400
        self._corps = corps
        self.text = json.dumps(corps)
        self.content = self.text.encode()

    def json(self):
        return self._corps


def _brancher(monkeypatch, status=200, corps=None):
    import requests
    monkeypatch.setattr(requests, "request",
                        lambda method, url, **kw: _Reponse(status, corps or {}))


def test_un_token_actif_avec_scope_passe(monkeypatch):
    _brancher(monkeypatch, corps={"active": True, "scope": "records:read records:write",
                                  "workspace_id": "w1", "workspace_name": "Acme"})
    A._verify(_fields("k"))  # ne lève pas


def test_un_token_INACTIF_est_un_echec_meme_avec_un_200(monkeypatch):
    """Le cas qui rendrait la sonde décorative : Attio répond 200, pas 401, pour
    un token révoqué — `{"active": false}` (contrat d'introspection RFC 7662)."""
    _brancher(monkeypatch, corps={"active": False})
    with pytest.raises(RuntimeError, match="INACTIF"):
        A._verify(_fields("k"))


def test_un_token_SANS_AUCUN_SCOPE_est_un_echec_nomme(monkeypatch):
    """Authentifie, mais ne peut rien faire — même leçon que la sonde Pennylane."""
    _brancher(monkeypatch, corps={"active": True, "scope": "",
                                  "workspace_id": "w1", "workspace_name": "Acme"})
    with pytest.raises(RuntimeError) as e:
        A._verify(_fields("k"))
    assert "AUCUN" in str(e.value) and "Acme" in str(e.value)


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, status=401, corps={"message": "invalid token"})
    with pytest.raises(Exception, match="401"):
        A._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    A.register(FastMCP("t"))
    assert cv.supports("attio")
    assert cv.probe_for("attio") is A._verify
    assert cv.couverture("attio") == cv.AUTH
