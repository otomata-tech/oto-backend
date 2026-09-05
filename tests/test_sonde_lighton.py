"""La sonde de connexion LightOn — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET workspaces` (déjà dans le client — `list_workspaces`) : opération de
gestion, pas de retrieval facturé (search/ask). Credential à 3 champs (ADR
0011, `base_url`/`workspace_id` optionnels), round-trip par le VRAI
`pack_secret`/`unpack_secret`.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import lighton as L


def _fields(**kw) -> dict:
    base = {"api_key": "k", "base_url": "", "workspace_id": ""}
    base.update(kw)
    secret = credentials_store.pack_secret("lighton", base)
    return credentials_store.unpack_secret("lighton", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_workspaces(self):
        self.appels.append("list_workspaces")
        if self._boom:
            raise self._boom
        return {"workspaces": []}


def _brancher(monkeypatch, client):
    import oto.tools.lighton as pkg
    monkeypatch.setattr(pkg, "LightOnClient", lambda **kw: client)
    return client


def test_un_credential_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    L._verify(_fields())
    assert cli.appels == ["list_workspaces"]


def test_LE_POINT__un_refus_leve_mais_pas_type(monkeypatch):
    """`LightOnClient._request` lève un `RuntimeError` NU — la sonde ne peut
    PAS viser `unauthorized` (aucun `status_code` sur l'exception)."""
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("LightOn 401: invalid key")))
    with pytest.raises(RuntimeError) as e:
        L._verify(_fields())
    assert not isinstance(e.value, cv.SondeRefusee)
    assert cv.classer(e.value) == cv.UNKNOWN


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    L.register(FastMCP("t"))
    assert cv.supports("lighton")
    assert cv.probe_for("lighton") is L._verify
    assert cv.couverture("lighton") == cv.AUTH
