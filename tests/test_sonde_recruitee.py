"""La sonde de connexion Recruitee — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /c/<company_id>/candidates` (déjà dans le client — `list_candidates`),
`limit=1`. Une liste VIDE est un état normal (aucun candidat), jamais un
refus — seul le fait que l'appel n'ait pas levé compte. Credential à 2 champs
(`api_token` + `company_id`, ADR 0011) — round-trip par le VRAI `pack_secret`/
`unpack_secret`, jamais un dict deviné.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import recruitee as R


def _fields(api_token: str, company_id: str) -> dict:
    secret = credentials_store.pack_secret(
        "recruitee", {"api_token": api_token, "company_id": company_id})
    return credentials_store.unpack_secret("recruitee", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_candidates(self, limit=None, offset=None, offer_id=None, query=None):
        self.appels.append(("list_candidates", limit))
        if self._boom:
            raise self._boom
        return {"candidates": []}


def _brancher(monkeypatch, client):
    import oto.tools.recruitee.client as rc
    monkeypatch.setattr(rc, "RecruiteeClient", lambda **kw: client)
    return client


def test_un_credential_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    R._verify(_fields("tok", "co-123"))
    assert cli.appels == [("list_candidates", 1)]


def test_un_credential_refuse_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid token")))
    with pytest.raises(RuntimeError, match="401"):
        R._verify(_fields("tok", "co-123"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    R.register(FastMCP("t"))
    assert cv.supports("recruitee")
    assert cv.probe_for("recruitee") is R._verify
    assert cv.couverture("recruitee") == cv.AUTH
