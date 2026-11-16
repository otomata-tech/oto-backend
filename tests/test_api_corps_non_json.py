"""Un succès au corps non JSON reste un SUCCÈS sur `api_get`, `api_post`, `api_doc`.

Vu en vrai sur une API publique : un 2xx rend « Pet deleted » sous un type JSON, le
client d'oto-core lève alors une erreur de décodage. La traduire en échec ferait
rejouer une écriture déjà faite chez le tiers. Un corps JSON garde sa forme intacte.
Le VRAI client d'oto-core est construit ; seul le transport (`Session.request`) est
simulé.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import requests
from fastmcp import FastMCP

from oto_mcp import egress
from oto_mcp.tools import api as H

CARTE = {"base_url": "https://api.acme.test", "auth_mode": "bearer", "token": "T",
         "doc_path": "/openapi.json"}


def _reponse(corps: bytes, statut: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code, r._content = statut, corps
    r.headers["Content-Type"] = "application/json"
    return r


@pytest.fixture()
def outils(monkeypatch):
    monkeypatch.setattr(H, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(H, "_resolve", lambda: SimpleNamespace(
        fields=dict(CARTE), entity_type="org", entity_id="1", account=""))
    monkeypatch.setattr(egress, "resolved_addresses", lambda h, p: {"93.184.216.34"})
    m = FastMCP("t")
    H.register(m)
    return lambda nom: asyncio.run(m.get_tool(nom)).fn


@pytest.fixture()
def amont(monkeypatch):
    etat = {"corps": b"Pet deleted", "statut": 200}
    monkeypatch.setattr(requests.Session, "request",
                        lambda self, *a, **k: _reponse(etat["corps"], etat["statut"]))
    return etat


@pytest.mark.parametrize("appel", [
    ("api_get", {"path": "/pet/1"}),
    ("api_post", {"path": "/pet", "body": {"name": "x"}}),
    ("api_doc", {}),
])
def test_un_succes_non_json_est_un_succes(outils, amont, appel):
    nom, kw = appel
    assert outils(nom)(**kw) == {"ok": True, "non_json_body": "Pet deleted",
                                 "truncated": False}


def test_un_long_corps_est_borne_et_le_dit(outils, amont):
    amont["corps"] = b"x" * (H.NON_JSON_MAX + 10)
    out = outils("api_get")(path="/a")
    assert len(out["non_json_body"]) == H.NON_JSON_MAX and out["truncated"] is True


@pytest.mark.parametrize("nom, kw", [("api_get", {"path": "/pet/1"}),
                                     ("api_post", {"path": "/pet", "body": {}})])
def test_un_corps_json_garde_sa_forme(outils, amont, nom, kw):
    amont["corps"] = b'{"id": 1, "name": "rex"}'
    assert outils(nom)(**kw) == {"id": 1, "name": "rex"}


def test_un_echec_amont_reste_un_echec(outils, amont):
    from oto_mcp.mcp_errors import McpError
    amont["corps"], amont["statut"] = b"not found", 404
    with pytest.raises(McpError, match="HTTP 404"):
        outils("api_get")(path="/pet/1")
