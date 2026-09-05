"""La sonde de connexion PromptWatch — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET projects` (déjà dans le client — `list_projects`), le premier appel de
découverte du connecteur. Credential à 2 champs (ADR 0011, `project_id`
optionnel), round-trip par le VRAI `pack_secret`/`unpack_secret`.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import promptwatch as P


def _fields(**kw) -> dict:
    base = {"api_key": "k", "project_id": ""}
    base.update(kw)
    secret = credentials_store.pack_secret("promptwatch", base)
    return credentials_store.unpack_secret("promptwatch", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_projects(self):
        self.appels.append("list_projects")
        if self._boom:
            raise self._boom
        return []


def _brancher(monkeypatch, client):
    import oto.tools.promptwatch.client as pc
    monkeypatch.setattr(pc, "PromptWatchClient", lambda **kw: client)
    return client


def test_un_credential_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    P._verify(_fields())
    assert cli.appels == ["list_projects"]


def test_un_credential_refuse_leve(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid key", service="promptwatch")))
    with pytest.raises(UpstreamHTTPError):
        P._verify(_fields())


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    P.register(FastMCP("t"))
    assert cv.supports("promptwatch")
    assert cv.probe_for("promptwatch") is P._verify
    assert cv.couverture("promptwatch") == cv.AUTH
