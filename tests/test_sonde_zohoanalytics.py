"""La sonde de connexion Zoho Analytics — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /restapi/v2/orgs` (déjà dans le client — `list_orgs`, le seul endpoint
qui ne réclame pas `ZANALYTICS-ORGID`). Credential à 5 champs (ADR 0011,
`refresh_token` optionnel), round-trip par le VRAI `pack_secret`/`unpack_secret`.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import zohoanalytics as Z


def _fields(**kw) -> dict:
    base = {
        "client_id": "id", "client_secret": "sec", "refresh_token": "ref",
        "org_id": "123", "data_center": "com",
    }
    base.update(kw)
    secret = credentials_store.pack_secret("zohoanalytics", base)
    return credentials_store.unpack_secret("zohoanalytics", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_orgs(self):
        self.appels.append("list_orgs")
        if self._boom:
            raise self._boom
        return [{"org_id": "123", "name": "Acme", "role": "admin"}]


def _brancher(monkeypatch, client):
    import oto.tools.zohoanalytics.client as zc
    monkeypatch.setattr(zc, "ZohoAnalyticsClient", lambda **kw: client)
    return client


def test_un_credential_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    Z._verify(_fields())
    assert cli.appels == ["list_orgs"]


def test_un_credential_refuse_leve(monkeypatch):
    from oto.tools.common.errors import UpstreamHTTPError
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid_client", service="zohoanalytics")))
    with pytest.raises(UpstreamHTTPError):
        Z._verify(_fields())


def test_data_center_manquant_leve_avant_appel_reseau(monkeypatch):
    """`_resolve_dc_domains` lève une McpError claire — pas de round-trip réseau
    juste pour découvrir que la région manque."""
    from oto_mcp.mcp_errors import McpError
    cli = _brancher(monkeypatch, _FauxClient())
    with pytest.raises(McpError):
        Z._verify(_fields(data_center=""))
    assert cli.appels == []


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    Z.register(FastMCP("t"))
    assert cv.supports("zohoanalytics")
    assert cv.probe_for("zohoanalytics") is Z._verify
    assert cv.couverture("zohoanalytics") == cv.AUTH
