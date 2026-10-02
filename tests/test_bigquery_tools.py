"""Tools `bigquery_*` (septième service Google, 2026-10-02).

Ce que ce fichier verrouille, parce que le scope `bigquery` PERMET d'écrire et de
dépenser — Google ne tient ni la lecture seule ni le budget à notre place :
1. LECTURE SEULE — le dry run passe d'abord, tout `statementType` autre que SELECT
   est refusé et la requête n'est JAMAIS lancée ;
2. COÛT — au-delà du plafond, refus avant lancement ; sinon `maximumBytesBilled` part
   avec la requête ; le plafond d'argument est lui-même borné ;
3. TEMPS — une requête pas finie rend `running` + `job_id`, reprise par `bigquery_results` ;
4. le PROJET DE FACTURATION — jamais deviné entre plusieurs ;
5. les refus Google NOMMÉS (raison BigQuery, pas le texte brut).

Faux client au seam `_client_for_user` : ni serveur, ni coffre, ni Google.
"""
import asyncio
import json
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from oto_mcp.mcp_errors import McpError

# Les tools importent `oto.tools.google.bigquery`, qui n'existe qu'au-delà du tag
# pinné : non concluant (pas rouge) tant que le venv retarde sur le pin.
pytestmark = pytest.mark.exige_pin_oto_core

GB = 1024 ** 3
SCHEMA = {"fields": [{"name": "month", "type": "DATE"}, {"name": "n", "type": "INTEGER"}]}


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import bigquery as T

    m = FastMCP("t")
    T.register(m)
    fn = asyncio.run(m.get_tool(name)).fn
    return lambda **kw: asyncio.run(fn(**kw))


@pytest.fixture
def client(monkeypatch):
    from oto_mcp.tools import bigquery as T

    inst = MagicMock()
    inst.accounts = []
    monkeypatch.setattr(T, "_client_for_user",
                        lambda account=None: (inst.accounts.append(account), inst)[1])
    inst.dry_run.return_value = {"statement_type": "SELECT", "bytes_processed": 3 * GB,
                                 "schema": SCHEMA, "referenced_tables": ["p.d.t"]}
    inst.query.return_value = {
        "jobComplete": True, "schema": SCHEMA, "totalRows": "2",
        "rows": [{"f": [{"v": "2026-01-01"}, {"v": "12"}]},
                 {"f": [{"v": "2026-02-01"}, {"v": "7"}]}],
        "totalBytesProcessed": str(3 * GB), "totalBytesBilled": str(3 * GB),
        "cacheHit": False,
        "jobReference": {"projectId": "bill", "jobId": "job_1", "location": "EU"}}
    return inst


def _http_error(status, reason, message):
    return HttpError(httplib2.Response({"status": status}), json.dumps(
        {"error": {"code": status, "message": message,
                   "errors": [{"reason": reason, "message": message}]}}).encode())


# --- 1. lecture seule ---------------------------------------------------------

@pytest.mark.parametrize("stype", ["DELETE", "INSERT", "MERGE", "CREATE_TABLE", "SCRIPT",
                                   "CALL", None])
def test_tout_ce_qui_nest_pas_un_select_est_refuse_sans_lancer(client, stype):
    client.dry_run.return_value = {**client.dry_run.return_value, "statement_type": stype}
    with pytest.raises(McpError, match="SELECT"):
        _tool("bigquery_query")(sql="DELETE FROM p.d.t WHERE true", project="bill")
    client.query.assert_not_called()


def test_meme_en_dry_run_un_non_select_est_refuse(client):
    client.dry_run.return_value = {**client.dry_run.return_value, "statement_type": "UPDATE"}
    with pytest.raises(McpError, match="SELECT"):
        _tool("bigquery_query")(sql="UPDATE p.d.t SET a=1 WHERE true", project="bill",
                                dry_run=True)


# --- 2. coût ------------------------------------------------------------------

def test_au_dela_du_plafond_refus_avant_lancement_estimation_a_lappui(client):
    client.dry_run.return_value = {**client.dry_run.return_value, "bytes_processed": 42 * GB}
    with pytest.raises(McpError, match="42 Go") as e:
        _tool("bigquery_query")(sql="SELECT * FROM p.d.t", project="bill")
    assert "10 Go" in str(e.value)
    client.query.assert_not_called()


def test_la_requete_part_avec_le_plafond_chez_google(client):
    out = _tool("bigquery_query")(sql="SELECT 1", project="bill", max_gb_billed=5)
    kw = client.query.call_args.kwargs
    assert kw["maximum_bytes_billed"] == 5 * GB
    assert kw["labels"] == {"source": "oto"}
    assert kw["timeout_ms"] < 45_000
    assert out["status"] == "done"


def test_le_plafond_par_defaut_est_10_go(client):
    _tool("bigquery_query")(sql="SELECT 1", project="bill")
    assert client.query.call_args.kwargs["maximum_bytes_billed"] == 10 * GB


@pytest.mark.parametrize("cap", [0, -1, 1025])
def test_le_plafond_dargument_est_borne(client, cap):
    with pytest.raises(McpError, match="max_gb_billed"):
        _tool("bigquery_query")(sql="SELECT 1", project="bill", max_gb_billed=cap)
    client.dry_run.assert_not_called()


def test_dry_run_estime_sans_lancer(client):
    out = _tool("bigquery_query")(sql="SELECT 1", project="bill", dry_run=True)
    assert out["dry_run"] is True and out["gb_processed"] == 3.0 and out["within_cap"]
    assert out["columns"] == [{"name": "month", "type": "DATE"}, {"name": "n", "type": "INTEGER"}]
    client.query.assert_not_called()


# --- 3. résultat, temps, pages ------------------------------------------------

def test_resultat_en_table_typee(client):
    out = _tool("bigquery_query")(sql="SELECT 1", project="bill")
    assert out["columns"] == [{"name": "month", "type": "DATE"}, {"name": "n", "type": "INTEGER"}]
    assert out["rows"] == [["2026-01-01", 12], ["2026-02-01", 7]]
    assert out["row_count"] == 2 and out["total_rows"] == 2
    assert out["gb_billed"] == 3.0 and out["cache_hit"] is False
    assert (out["job_id"], out["location"], out["project"]) == ("job_1", "EU", "bill")
    assert "page_token" not in out


def test_requete_pas_finie_rend_le_job_a_reprendre(client):
    client.query.return_value = {"jobComplete": False,
                                 "jobReference": {"projectId": "bill", "jobId": "job_2",
                                                  "location": "europe-west1"}}
    out = _tool("bigquery_query")(sql="SELECT 1", project="bill")
    assert out["status"] == "running" and out["job_id"] == "job_2"
    assert out["location"] == "europe-west1" and "bigquery_results" in out["hint"]


def test_lignes_tronquees_donnent_la_page_suivante(client):
    client.query.return_value = {**client.query.return_value, "pageToken": "pt2",
                                 "totalRows": "5000"}
    out = _tool("bigquery_query")(sql="SELECT 1", project="bill", max_rows=2)
    assert out["page_token"] == "pt2" and "bigquery_results" in out["hint"]


def test_max_rows_est_plafonne(client):
    _tool("bigquery_query")(sql="SELECT 1", project="bill", max_rows=50_000)
    assert client.query.call_args.kwargs["max_results"] == 1000


def test_results_transmet_job_location_et_page(client):
    client.get_query_results.return_value = client.query.return_value
    out = _tool("bigquery_results")(job_id="job_1", project="bill", location="EU",
                                    page_token="pt2", max_rows=10)
    args, kw = client.get_query_results.call_args
    assert args == ("bill", "job_1")
    assert kw["location"] == "EU" and kw["page_token"] == "pt2" and kw["max_results"] == 10
    assert out["status"] == "done"


def test_params_vides_refuses_avant_tout_appel(client):
    with pytest.raises(McpError, match="empty list"):
        _tool("bigquery_query")(sql="SELECT 1", project="bill", params={"ids": []})
    client.dry_run.assert_not_called()


# --- 4. projet de facturation -------------------------------------------------

def test_sans_projet_le_seul_visible_est_pris(client):
    client.list_projects.return_value = {"projects": [{"id": "solo"}], "next_page_token": None}
    _tool("bigquery_query")(sql="SELECT 1")
    assert client.dry_run.call_args.args[1] == "solo"


def test_sans_projet_plusieurs_visibles_refus_qui_les_nomme(client):
    client.list_projects.return_value = {"projects": [{"id": "a"}, {"id": "b"}],
                                         "next_page_token": None}
    with pytest.raises(McpError, match="a, b"):
        _tool("bigquery_query")(sql="SELECT 1")
    client.dry_run.assert_not_called()


# --- 5. refus nommés ----------------------------------------------------------

def test_sql_invalide_renvoie_vers_le_schema(client):
    client.dry_run.side_effect = _http_error(400, "invalidQuery", "Unrecognized name: foo at [1:8]")
    with pytest.raises(McpError, match="Unrecognized name: foo") as e:
        _tool("bigquery_query")(sql="SELECT foo", project="bill")
    assert "bigquery_table" in str(e.value)


def test_pas_le_droit_de_lancer_un_job_nomme_le_role(client):
    client.dry_run.side_effect = _http_error(
        403, "accessDenied",
        "Access Denied: Project bill: User does not have bigquery.jobs.create permission")
    with pytest.raises(McpError, match="BigQuery Job User"):
        _tool("bigquery_query")(sql="SELECT 1", project="bill")


def test_api_non_activee_dit_que_cest_le_client_oauth(client):
    client.list_projects.side_effect = _http_error(
        403, "accessNotConfigured",
        "BigQuery API has not been used in project 123 before or it is disabled.")
    with pytest.raises(McpError, match="client OAuth"):
        _tool("bigquery_catalog")()


def test_un_compte_sans_le_service_est_refuse_en_nommant_la_carte(monkeypatch):
    """Le refus vient de `credentials_for(service="bigquery")` — le tool le relaie."""
    from oto_mcp import access
    from oto_mcp.auth import google as G
    from oto_mcp.tools import bigquery as T

    vus = {}
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "sub-1")

    def faux(sub, account=None, service=None):
        vus["service"] = service
        raise RuntimeError("Le compte Google a@b.c n'a pas encore autorisé Google BigQuery")

    monkeypatch.setattr(G, "credentials_for", faux)
    with pytest.raises(McpError, match="Google BigQuery"):
        T._client_for_user()
    assert vus["service"] == "bigquery"


# --- catalogue et table -------------------------------------------------------

def test_catalogue_un_niveau_par_appel(client):
    client.list_projects.return_value = {"projects": [{"id": "p"}], "next_page_token": None}
    client.list_datasets.return_value = {"datasets": [{"id": "d"}], "next_page_token": None}
    client.list_tables.return_value = {"tables": [{"id": "t"}], "total": 1,
                                       "next_page_token": None}
    assert _tool("bigquery_catalog")()["level"] == "projects"
    assert _tool("bigquery_catalog")(project="p")["level"] == "datasets"
    assert _tool("bigquery_catalog")(project="p", dataset="d")["level"] == "tables"
    with pytest.raises(McpError):
        _tool("bigquery_catalog")(dataset="d")


def test_table_schema_et_apercu_gratuit(client):
    client.get_table.return_value = {
        "type": "TABLE", "schema": SCHEMA, "numRows": "2", "numBytes": str(GB),
        "timePartitioning": {"type": "DAY", "field": "month"}, "location": "EU"}
    client.list_rows.return_value = {"rows": [{"f": [{"v": "2026-01-01"}, {"v": "12"}]}]}
    out = _tool("bigquery_table")(table="`p.d.t`", preview_rows=1)
    assert client.get_table.call_args.args == ("p", "d", "t")
    assert out["columns"][1] == {"name": "n", "type": "INTEGER", "mode": "NULLABLE"}
    assert out["num_rows"] == 2 and out["gb"] == 1.0
    assert out["partitioning"]["field"] == "month"
    assert out["preview"] == [["2026-01-01", 12]]
    client.query.assert_not_called()


@pytest.mark.parametrize("kind", ["VIEW", "EXTERNAL", "MATERIALIZED_VIEW"])
def test_pas_dapercu_hors_table_stockee(client, kind):
    client.get_table.return_value = {"type": kind, "schema": SCHEMA,
                                     "view": {"query": "SELECT 1"}}
    out = _tool("bigquery_table")(table="p.d.v", preview_rows=5)
    assert "preview" not in out and kind.lower() in out["preview_note"]
    assert out["view_sql"] == "SELECT 1"
    client.list_rows.assert_not_called()


def test_reference_de_table_invalide(client):
    with pytest.raises(McpError, match="project.dataset.table"):
        _tool("bigquery_table")(table="juste_une_table")


# --- surface ------------------------------------------------------------------

def test_chaque_tool_a_une_description():
    from fastmcp import FastMCP
    from oto_mcp.tools import bigquery as T

    m = FastMCP("t")
    T.register(m)
    tools = {t.name: t for t in asyncio.run(m._list_tools())}
    assert set(tools) == {"bigquery_catalog", "bigquery_table", "bigquery_query",
                          "bigquery_results"}
    for t in tools.values():
        assert t.description and len(t.description) > 80, t.name
