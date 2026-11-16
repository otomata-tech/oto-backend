"""La sonde de connexion SearchApi — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /api/v1/me` — endpoint dédié "account usage", gratuit (documenté "without
requiring a specific plan level"), distinct de `/api/v1/search` (facturé) que
ce connecteur wrappe par ailleurs. Bearer header, jamais en query (#284). Appelé par
`SearchApiClient.account` d'oto-core (v1.233.0).
"""
from __future__ import annotations

import pytest
import requests
from oto.tools.common.errors import UpstreamHTTPError

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import searchapi as S


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("searchapi", secret)


class _FauxResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text, self.headers = "", {}

    def json(self):
        return {}


def test_une_cle_valide_passe(monkeypatch):
    vu = {}

    def _get(self, url, params=None, headers=None, timeout=None):
        vu["url"] = url
        vu["headers"] = headers
        return _FauxResponse(200)

    monkeypatch.setattr(requests.Session, "get", _get)
    S._verify(_fields("k"))
    assert vu["url"] == "https://www.searchapi.io/api/v1/me"
    assert vu["headers"] == {"Authorization": "Bearer k"}


def test_une_cle_refusee_leve(monkeypatch):
    """Le refus porte son statut (`UpstreamHTTPError.status_code`) : c'est lui que lit
    la capacité verify, jamais le texte."""
    monkeypatch.setattr(requests.Session, "get", lambda self, *a, **k: _FauxResponse(401))
    with pytest.raises(UpstreamHTTPError) as e:
        S._verify(_fields("k"))
    assert e.value.status_code == 401


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    S.register(FastMCP("t"))
    assert cv.supports("searchapi")
    assert cv.probe_for("searchapi") is S._verify
    assert cv.couverture("searchapi") == cv.AUTH
