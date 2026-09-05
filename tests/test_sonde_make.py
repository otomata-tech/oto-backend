"""La sonde de connexion Make — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /organizations` (déjà dans le client — `list_organizations`), le premier
appel de découverte du connecteur. Credential à 2 champs (ADR 0011,
`api_token` + `base_url`), round-trip par le VRAI `pack_secret`/`unpack_secret`.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import make as M


def _fields(api_token: str, base_url: str) -> dict:
    secret = credentials_store.pack_secret(
        "make", {"api_token": api_token, "base_url": base_url})
    return credentials_store.unpack_secret("make", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_organizations(self):
        self.appels.append("list_organizations")
        if self._boom:
            raise self._boom
        return {"organizations": []}


def _brancher(monkeypatch, client):
    import oto.tools.make as pkg
    monkeypatch.setattr(pkg, "MakeClient", lambda **kw: client)
    return client


def test_un_credential_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    M._verify(_fields("tok", "https://eu1.make.com"))
    assert cli.appels == ["list_organizations"]


def test_un_credential_refuse_leve(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid token", service="make")))
    with pytest.raises(UpstreamHTTPError):
        M._verify(_fields("tok", "https://eu1.make.com"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    M.register(FastMCP("t"))
    assert cv.supports("make")
    assert cv.probe_for("make") is M._verify
    assert cv.couverture("make") == cv.AUTH
