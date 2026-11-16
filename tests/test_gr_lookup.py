"""L'enveloppe MCP `gr_lookup` : le client (GEMI + VIES) vit dans oto-core depuis
v1.233.0 (`oto.tools.gr.GrClient`, oto-now/connectors#244) et s'y teste. Ici, ce que
l'hôte garantit : une requête vide est refusée en `INVALID_PARAMS` avant tout appel, et
une panne VIES reste NOMMÉE dans la réponse (`vat_check_error`), jamais lue comme un
numéro invalide. Aucun appel réseau.
"""
import asyncio

import pytest
import requests
from mcp.types import INVALID_PARAMS

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import gr as G


def _tool():
    from fastmcp import FastMCP

    m = FastMCP("t")
    G.register(m)
    return asyncio.run(m.get_tool("gr_lookup")).fn


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code
        self.text, self.headers = "", {}

    def json(self):
        return self._payload


@pytest.mark.parametrize("q", ["", "   "])
def test_an_empty_query_is_refused_before_any_call(monkeypatch, q):
    monkeypatch.setattr(requests.Session, "get",
                        lambda *a, **k: pytest.fail("appel réseau inattendu"))
    with pytest.raises(McpError, match="empty") as e:
        _tool()(query=q)
    assert e.value.error.code == INVALID_PARAMS


def test_a_vies_outage_is_named_never_read_as_invalid(monkeypatch):
    one = {"payload": {"autocomplete": [{"co_name": "ACME AE", "arGemi": 123,
                                         "afm": "094014201", "companyStatusId": 3}]}}

    def _get(self, url, **k):
        return _Resp(one) if "autocomplete" in url else _Resp({}, status_code=503)

    monkeypatch.setattr(requests.Session, "get", _get)
    out = _tool()(query="EL094014201")
    row = out["results"][0]
    assert (out["count"], row["vat"], row["active"]) == (1, "EL094014201", True)
    assert "VIES unavailable" in row["vat_check_error"]
    assert "vat_valid" not in row
