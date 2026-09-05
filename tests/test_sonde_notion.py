"""La sonde de connexion Notion — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /v1/users/me` (« Retrieve your token's bot user »). Notion n'accorde pas
de scope granulaire par jeton d'intégration (seulement un partage par page/base
côté workspace, invisible depuis l'API) — rien de plus fin à distinguer ici.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import notion as N


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("notion", secret)


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom
        self.appels = []

    def _request(self, method, endpoint, **kw):
        self.appels.append((method, endpoint))
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.notion.lib.notion_client as nc
    monkeypatch.setattr(nc, "NotionClient", lambda **kw: client)
    return client


def test_un_bot_user_identifie_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient(
        {"object": "user", "id": "u1", "type": "bot", "bot": {"owner": {"type": "workspace"}}}))
    N._verify(_fields("k"))
    assert cli.appels == [("GET", "users/me")], "un seul appel, en lecture"


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: unauthorized")))
    with pytest.raises(RuntimeError, match="401"):
        N._verify(_fields("k"))


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    _brancher(monkeypatch, _FauxClient({}))
    with pytest.raises(RuntimeError, match="sans identifier"):
        N._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    N.register(FastMCP("t"))
    assert cv.supports("notion")
    assert cv.probe_for("notion") is N._verify
    assert cv.couverture("notion") == cv.AUTH
