"""La sonde de connexion Forager — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /api/users/current/` (déjà dans le client — `get_current_user`),
documenté « Free » (client oto-core ET `forager_account(op="me")`). Pas
`auth+quota` : `credits_balance` vit par compte dans `accounts[]`, une clé
multi-comptes n'en désigne aucun par défaut.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import forager as F


def _fields(api_key: str) -> dict:
    secret = credentials_store.pack_secret("forager", {"api_key": api_key})
    return credentials_store.unpack_secret("forager", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def get_current_user(self):
        self.appels.append("get_current_user")
        if self._boom:
            raise self._boom
        return {"accounts": [{"id": 1, "credits_balance": 42}]}


def _brancher(monkeypatch, client):
    import oto.tools.forager as pkg
    monkeypatch.setattr(pkg, "ForagerClient", lambda **kw: client)
    return client


def test_un_credential_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    F._verify(_fields("k"))
    assert cli.appels == ["get_current_user"]


def test_un_credential_refuse_leve(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid key", service="forager")))
    with pytest.raises(UpstreamHTTPError):
        F._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    F.register(FastMCP("t"))
    assert cv.supports("forager")
    assert cv.probe_for("forager") is F._verify
    assert cv.couverture("forager") == cv.AUTH
