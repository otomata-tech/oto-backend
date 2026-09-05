"""La sonde de connexion Reddit (passerelle redditapis.com) — otomata-tech/oto#69.
Couvre `auth` SEUL.

`GET /api/reddit/search/communities` (déjà dans le client — `search_subreddits`),
une recherche générique qui ne dépend d'aucune cible existante.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import reddit as R


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("reddit", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def search_subreddits(self, query, limit=25):
        self.appels.append((query, limit))
        if self._boom:
            raise self._boom
        return {"items": [], "after": None, "source": "redditapis"}


def _brancher(monkeypatch, client):
    import oto.tools.reddit as pkg
    monkeypatch.setattr(pkg, "RedditClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    R._verify(_fields("k"))
    assert cli.appels == [("a", 1)]


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("redditapis: invalid key")))
    with pytest.raises(RuntimeError, match="invalid key"):
        R._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    R.register(FastMCP("t"))
    assert cv.supports("reddit")
    assert cv.probe_for("reddit") is R._verify
    assert cv.couverture("reddit") == cv.AUTH
