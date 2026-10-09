"""Connecteur BODACC (annonces légales, open data DILA) — verrouille : l'entrée registre
(open data, AUCUN credential, testable depuis le dashboard), la surface MCP (UN outil,
le verbe en `op`, décrit), la jointure tool↔client oto-core, et le contrat tenu à
travers le tool layer sur un amont mocké : filtres composés, aiguillage des trois ops,
refus actionnables avant tout appel, amont en erreur rendu lisible.
"""
import asyncio
import json

import pytest
import requests
from oto_mcp import providers
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import TESTABLE_NAMESPACES, namespace_of


def _tool(name="bodacc_notice"):
    from fastmcp import FastMCP
    from oto_mcp.tools import bodacc as bodacc_tool

    m = FastMCP("t")
    bodacc_tool.register(m)
    return asyncio.run(m.get_tool(name))


def _call(**kwargs):
    return _tool().fn(**kwargs)


# --- registre -----------------------------------------------------------------

def test_bodacc_is_open_data_without_credential():
    c = providers.REGISTRY["bodacc"]
    assert c.kind == "tools" and c.family == "open-data"
    assert c.secret_kind == "none" and not c.keyed and c.auth_method == "none"
    assert "bodacc" not in providers.KEY_PROVIDERS
    assert "bodacc" not in providers.CREDENTIAL_PROVIDERS
    assert c.default_active is False


def test_bodacc_is_testable_from_dashboard():
    # Lecture seule, sans clé ni quota à nous : un « tester » ne coûte rien à personne.
    assert "bodacc" in TESTABLE_NAMESPACES


def test_bodacc_doc_is_served():
    kinds = [s.kind for s in providers.REGISTRY["bodacc"].doc_sections]
    assert "usage" in kinds and "prerequisite" not in kinds


# --- surface MCP --------------------------------------------------------------

def test_tool_registers_under_namespace_with_description():
    t = _tool()
    assert namespace_of(t.name) == "bodacc"
    assert t.description and "fr_events" in t.description
    assert "Args:" not in t.description


def test_client_exposes_methods_called_by_tools():
    from oto.tools.bodacc.notices import BodaccNoticesClient
    for m in ("search", "count", "get"):
        assert callable(getattr(BodaccNoticesClient, m, None))


# --- contrat à travers le tool layer (amont mocké) -----------------------------

class _Resp:
    def __init__(self, body, status=200):
        self.status_code, self._body = status, body
        self.content, self.text = b"x", str(body)

    def json(self):
        return self._body


_NOTICE = {
    "id": "A1", "dateparution": "2026-10-09", "familleavis": "collective",
    "typeavis": "annonce", "registre": ["811038504"], "commercant": "ACME",
    "numerodepartement": "69",
    "jugement": json.dumps({"nature": "Jugement d'ouverture", "date": "2026-10-01",
                            "complementJugement": "Redressement judiciaire."}),
}


@pytest.fixture()
def upstream(monkeypatch):
    from oto.tools.bodacc import notices

    state = {"calls": [], "body": {"total_count": 0, "results": []}, "status": 200,
             "exc": None}

    def fake_get(self, url, **kwargs):
        state["calls"].append(kwargs["params"])
        if state["exc"]:
            raise state["exc"]
        return _Resp(state["body"], state["status"])

    monkeypatch.setattr(notices.requests.Session, "get", fake_get)
    return state


def test_search_par_defaut_compose_les_filtres(upstream):
    upstream["body"] = {"total_count": 1, "results": [_NOTICE]}
    out = _call(famille="collective", departement="69", date_from="2026-10-05")
    assert upstream["calls"][0]["where"] == (
        'familleavis="collective" AND numerodepartement="69" '
        'AND dateparution>="2026-10-05"')
    assert out["source"].startswith("BODACC")
    line = out["notices"][0]
    assert line["siren"] == "811038504" and line["resume"] == "Redressement judiciaire."
    assert out["next_offset"] is None


def test_count_exige_group_by(upstream):
    with pytest.raises(McpError, match="group_by"):
        _call(op="count")
    assert upstream["calls"] == []


def test_count_groupe(upstream):
    upstream["body"] = {"total_count": 2, "results": [{"cle": "2026-09", "n": 5},
                                                      {"cle": "2026-10", "n": 7}]}
    out = _call(op="count", group_by="mois", famille="vente")
    assert upstream["calls"][0]["where"] == 'familleavis="vente"'
    assert [g["count"] for g in out["groups"]] == [5, 7]
    assert out["notices_counted"] == 12


def test_get_exige_id_et_dit_l_introuvable(upstream):
    with pytest.raises(McpError, match="`id`"):
        _call(op="get")
    with pytest.raises(McpError, match="No BODACC notice"):
        _call(op="get", id="NOPE")


def test_get_rend_l_annonce_complete(upstream):
    upstream["body"] = {"total_count": 1, "results": [_NOTICE]}
    out = _call(op="get", id="A1")
    assert out["notice"]["jugement"]["nature"] == "Jugement d'ouverture"


def test_filtre_invalide_refuse_avant_appel(upstream):
    with pytest.raises(McpError, match="YYYY-MM-DD"):
        _call(date_from="09/10/2026")
    with pytest.raises(McpError, match="10000"):
        _call(limit=100, offset=9990)
    assert upstream["calls"] == []


def test_amont_en_erreur_rendu_lisible(upstream):
    upstream["status"] = 400
    upstream["body"] = {"error_code": "ODSQLSyntaxError", "message": "unexpected token"}
    with pytest.raises(McpError, match="unexpected token"):
        _call(q="x")
    upstream["status"] = 503
    with pytest.raises(McpError, match="temporarily unavailable"):
        _call(q="x")


def test_amont_injoignable_rendu_lisible(upstream):
    upstream["exc"] = requests.ConnectTimeout("boom")
    with pytest.raises(McpError, match="could not be reached"):
        _call()
