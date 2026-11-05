"""Tool `powerbi` du connecteur Power BI (service du porteur `microsoft`).

Ce que ce fichier verrouille :
- la SURFACE : un seul outil, en lecture ; le jeton du service `powerbi` (ressource Power
  BI, demandé à part de Graph par `auth/microsoft`) ;
- le dispatch vers les VRAIES méthodes de `PowerBIClient` (autospec) ;
- une requête DAX bornée en lignes et dite tronquée ;
- les mesures sans formule DITES comme telles (droit d'écriture sur le modèle) ;
- les arguments d'une autre op refusés avant tout appel.
"""
import asyncio
from unittest.mock import create_autospec

import pytest
from oto_mcp.mcp_errors import McpError

pytestmark = pytest.mark.exige_pin_oto_core


@pytest.fixture
def client(monkeypatch):
    import oto.tools.microsoft as coeur

    inst = create_autospec(coeur.PowerBIClient, instance=True)
    jetons = []
    monkeypatch.setattr(coeur, "PowerBIClient", lambda jeton: jetons.append(jeton) or inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")

    def jeton(sub, service):
        assert service == "powerbi"
        return f"AT-{sub}"

    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for", jeton)
    inst.jetons = jetons
    return inst


def _tool():
    from fastmcp import FastMCP
    from oto_mcp.tools import powerbi as X

    m = FastMCP("t")
    X.register(m)
    return asyncio.run(m.get_tool("powerbi")).fn


def test_un_seul_outil_en_lecture():
    from fastmcp import FastMCP
    from oto_mcp.tools import powerbi as X
    from oto_mcp.tools.lecture import LECTURE

    m = FastMCP("t")
    X.register(m)
    outils = {t.name: t for t in asyncio.run(m.list_tools())}
    assert list(outils) == ["powerbi"] and outils["powerbi"].annotations == LECTURE


def test_espaces_rapports_modeles(client):
    client.list_workspaces.return_value = [{"id": "w1", "name": "Pilotage", "type": "Workspace",
                                            "isReadOnly": False, "extra": 1}]
    client.list_reports.return_value = [{"id": "r1", "name": "Activité", "datasetId": "d1",
                                         "reportType": "PowerBIReport", "webUrl": "u"}]
    client.list_datasets.return_value = [{"id": "d1", "name": "Activité"}]
    assert _tool()()["workspaces"] == [{"id": "w1", "name": "Pilotage", "type": "Workspace",
                                        "isReadOnly": False}]
    out = _tool()(op="reports", workspace_id="w1")
    assert out["reports"][0]["datasetId"] == "d1"
    client.list_reports.assert_called_once_with("w1")
    assert _tool()(op="datasets", fields=["name"])["datasets"] == [{"name": "Activité"}]
    client.list_datasets.assert_called_once_with(None)
    assert client.jetons == ["AT-sub-1"] * 3


def test_une_requete_dax_bornee(client):
    client.execute_query.return_value = {"columns": ["Mois", "CA"],
                                         "rows": [{"Mois": i, "CA": 1} for i in range(10)],
                                         "truncated": False, "error": None}
    out = _tool()(op="query", workspace_id="w1", dataset_id="d1",
                  dax="EVALUATE T", max_rows=3)
    client.execute_query.assert_called_once_with("d1", "EVALUATE T", workspace_id="w1")
    assert len(out["rows"]) == 3 and out["row_count"] == 10 and out["truncated"] is True


@pytest.mark.parametrize("visible", [True, False])
def test_les_mesures_disent_quand_la_formule_manque(client, visible):
    mesure = {"Name": "CA", "Table": "Ventes", "Expression": "SUM(x)" if visible else ""}
    client.list_measures.return_value = {"measures": [mesure], "expression_visible": visible,
                                         "truncated": False}
    out = _tool()(op="measures", dataset_id="d1")
    client.list_measures.assert_called_once_with("d1", workspace_id=None)
    assert out["expression_visible"] is visible and out["count"] == 1
    assert ("note" in out) is (not visible)
    if not visible:
        assert "write permission" in out["note"]


@pytest.mark.parametrize("kwargs, motif", [
    ({"op": "query", "dataset_id": "d1"}, "dax"),
    ({"op": "measures"}, "dataset_id"),
    ({"op": "report_pages"}, "report_id"),
    ({"op": "workspaces", "dataset_id": "d1"}, "dataset_id"),
    ({"op": "measures", "dataset_id": "d1", "dax": "EVALUATE T"}, "dax"),
    ({"op": "reports", "max_rows": 10}, "max_rows"),
    ({"op": "query", "dataset_id": "d1", "dax": "EVALUATE T", "max_rows": 5000}, "max_rows"),
    ({"op": "query", "dataset_id": "d1", "dax": "EVALUATE T", "full": True}, "full"),
    ({"op": "export"}, "op must be"),
])
def test_refus_avant_tout_appel(client, kwargs, motif):
    with pytest.raises(McpError, match=motif):
        _tool()(**kwargs)
    assert client.jetons == []
