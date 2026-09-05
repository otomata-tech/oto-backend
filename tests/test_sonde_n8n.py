"""La sonde de connexion n8n — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /workflows` (déjà dans le client — `list_workflows`), `limit=1`. Credential
à 2 champs (ADR 0011, `api_key` + `base_url`), round-trip par le VRAI
`pack_secret`/`unpack_secret`. `N8nClient` lève un `Exception` NU (pas de
`status_code` typé) — la sonde ne peut donc pas viser `unauthorized`, seulement
« ça a levé, ou pas ».
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import n8n as N


def _fields(api_key: str, base_url: str) -> dict:
    secret = credentials_store.pack_secret(
        "n8n", {"api_key": api_key, "base_url": base_url})
    return credentials_store.unpack_secret("n8n", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_workflows(self, limit=None, active=None, tags=None, cursor=None):
        self.appels.append(("list_workflows", limit))
        if self._boom:
            raise self._boom
        return {"data": []}


def _brancher(monkeypatch, client):
    import oto.tools.n8n as pkg
    monkeypatch.setattr(pkg, "N8nClient", lambda **kw: client)
    return client


def test_un_credential_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    N._verify(_fields("k", "https://acme.app.n8n.cloud"))
    assert cli.appels == [("list_workflows", 1)]


def test_un_credential_refuse_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=Exception("n8n HTTP 401: unauthorized")))
    with pytest.raises(Exception, match="401"):
        N._verify(_fields("k", "https://acme.app.n8n.cloud"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    N.register(FastMCP("t"))
    assert cv.supports("n8n")
    assert cv.probe_for("n8n") is N._verify
    assert cv.couverture("n8n") == cv.AUTH
