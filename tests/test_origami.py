"""Connecteur Origami — campagnes email + LinkedIn (origami.chat, API v2).

Verrouille : l'entrée de registre (keyed byo-only, Prospection, hors socle), la surface
MCP sous le namespace `origami` (12 tools, chacun avec une description — régression du
piège f-string-docstring), la jointure tool↔client oto-core (garde version-skew), la
sonde « tester la connexion », et surtout la convention `dry_run` sur CHAQUE tool
mutant : la validation tourne, l'appel final est sauté, la réponse porte `dry_run:
true` + un aperçu — et le lancement (le geste qui ENVOIE) est dry_run=True PAR DÉFAUT.

Le CLIENT est mocké (`oto.tools.origami.client.OrigamiClient`), pas `requests` : on
teste le contrat du tool layer, le contrat HTTP vit dans oto-core.
"""
import asyncio
import base64
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "origami_workspaces", "origami_tables", "origami_rows", "origami_upload_csv",
    "origami_campaigns", "origami_campaign_create", "origami_run_get",
    "origami_campaign_launch", "origami_campaign_pause", "origami_campaign_resume",
    "origami_campaign_delete", "origami_sequences",
}
# Les tools qui ÉCRIVENT : tous doivent accepter `dry_run`.
MUTATING_TOOLS = {
    "origami_workspaces", "origami_rows", "origami_upload_csv", "origami_campaign_create",
    "origami_campaign_launch", "origami_campaign_pause", "origami_campaign_resume",
    "origami_campaign_delete",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("og_live_k", False))


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import origami

    m = FastMCP("t")
    origami.register(m)
    return asyncio.run(m.get_tool(name))


def _upstream(status, body):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body, service="origami")


# --- registre -----------------------------------------------------------------

def test_origami_is_keyed_byo_only_connector():
    c = providers.REGISTRY["origami"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False               # deny-by-default
    assert c.default_quota == 0
    assert "origami" in providers.KEY_PROVIDERS
    assert c.category == "Outreach"
    assert c.publisher_name == "Origami"
    assert c.label == "Origami"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["origami"] == "origami.chat"


def test_origami_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["origami"].doc_sections}
    assert {"prerequisite", "usage", "note"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_origami_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "origami" for t in all_tools if t.startswith("origami_"))


def test_origami_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_every_mutating_tool_takes_dry_run(all_tools):
    for name in MUTATING_TOOLS:
        props = all_tools[name].parameters["properties"]
        assert "dry_run" in props, f"{name} n'accepte pas dry_run"


def test_launch_is_dry_run_by_default(all_tools):
    props = all_tools["origami_campaign_launch"].parameters["properties"]
    assert props["dry_run"]["default"] is True
    # Les autres tools mutants sont dry_run=False par défaut (le lancement est le seul
    # geste qui envoie vers des inconnus).
    for name in MUTATING_TOOLS - {"origami_campaign_launch"}:
        assert all_tools[name].parameters["properties"]["dry_run"]["default"] is False, name


def test_docstrings_carry_the_gotchas(all_tools):
    d = all_tools["origami_campaign_create"].description
    assert "deleted" in d and "never-sent" in d          # block_prior_contacts, même brouillons
    d = all_tools["origami_campaign_launch"].description
    assert "missingChannels" in d and "dry_run=False" in d
    d = all_tools["origami_campaign_delete"].description
    assert "404" in d
    d = all_tools["origami_rows"].description
    assert "UNKNOWN_FIELDS" in d and "slug" in d.lower()
    d = all_tools["origami_run_get"].description
    assert "GET /runs" in d
    d = all_tools["origami_upload_csv"].description
    assert "multipart" in d


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("origami")


def test_verify_probe_is_a_read():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        from oto_mcp.tools import origami
        origami._verify({"key": "og_live_k"}, {})
    cls.assert_called_once_with(api_key="og_live_k")
    cls.return_value.list_workspaces.assert_called_once()


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.origami.client import OrigamiClient
    for meth in ("list_workspaces", "create_workspace", "upload_documents", "list_tables",
                 "get_table", "list_columns", "list_rows", "upsert_rows", "list_campaigns",
                 "get_campaign", "campaign_stats", "campaign_people", "create_campaign",
                 "get_run", "launch_campaign", "pause_campaign", "resume_campaign",
                 "delete_campaign", "list_sequences", "get_sequence"):
        assert callable(getattr(OrigamiClient, meth, None)), f"OrigamiClient.{meth} manquant"


# --- workspaces / tables ------------------------------------------------------

def test_workspaces_list_and_create_dry_run():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_workspaces.return_value = {"items": [{"id": "ws"}], "nextCursor": None}
        assert _tool("origami_workspaces").fn(search="tul")["items"] == [{"id": "ws"}]
        inst.list_workspaces.assert_called_once_with(cursor=None, search="tul")

        out = _tool("origami_workspaces").fn(op="create", name=" Pilote ", dry_run=True)
        assert out == {"dry_run": True, "would_create": {"name": "Pilote"}}
        inst.create_workspace.assert_not_called()

        _tool("origami_workspaces").fn(op="create", name="Pilote")
        inst.create_workspace.assert_called_once_with("Pilote")

        with pytest.raises(McpError):
            _tool("origami_workspaces").fn(op="create")   # name manquant


def test_tables_ops():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        _tool("origami_tables").fn(workspace_id="ws-1", cursor="c")
        inst.list_tables.assert_called_once_with(workspace_id="ws-1", cursor="c")
        _tool("origami_tables").fn(op="get", table_id="t-1", include_stats=True)
        inst.get_table.assert_called_once_with("t-1", include="stats")
        _tool("origami_tables").fn(op="columns", table_id="t-1")
        inst.list_columns.assert_called_once_with("t-1")
        with pytest.raises(McpError):
            _tool("origami_tables").fn(op="get")


# --- rows : list suit nextCursor, upsert gaté ---------------------------------

def test_rows_list_follows_next_cursor_up_to_max_pages():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_rows.side_effect = [
            {"items": [{"email": "a"}], "nextCursor": "c2", "total": 3},
            {"items": [{"email": "b"}], "nextCursor": "c3", "total": 3},
            {"items": [{"email": "c"}], "nextCursor": None, "total": 3},
        ]
        out = _tool("origami_rows").fn(op="list", table_id="t-1", max_pages=2)
    assert out["count"] == 2 and out["pages_fetched"] == 2
    assert out["truncated"] is True and out["cursor"] == "c3"
    assert out["total"] == 3
    assert [c.kwargs["cursor"] for c in inst.list_rows.call_args_list] == [None, "c2"]
    assert all(c.kwargs["cells"] == "flat" for c in inst.list_rows.call_args_list)


def test_rows_list_stops_when_cursor_ends():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        cls.return_value.list_rows.return_value = {"items": [{"email": "a"}],
                                                   "nextCursor": None, "total": 1}
        out = _tool("origami_rows").fn(op="list", table_id="t-1", max_pages=20)
    assert out["count"] == 1 and out["pages_fetched"] == 1
    assert out["truncated"] is False and out["cursor"] is None


_COLUMNS = {"items": [
    {"id": "1", "name": "Email", "slug": "email", "kind": "input"},
    {"id": "2", "name": "First name", "slug": "first-name", "kind": "input"},
    {"id": "3", "name": "Fit score", "slug": "fit-score", "kind": "score"},
]}


def test_rows_upsert_dry_run_validates_slugs_and_writes_nothing():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_columns.return_value = _COLUMNS
        out = _tool("origami_rows").fn(
            op="upsert", table_id="t-1", rows=[{"email": "a@b.fr", "first-name": "A"}],
            match_columns=["email"], dry_run=True)
    assert out["dry_run"] is True
    assert out["would_upsert"]["rows"] == 1
    assert out["would_upsert"]["match_columns"] == ["email"]
    assert out["would_upsert"]["enrich"] is False
    assert out["check"]["columns_available"] is True
    inst.list_columns.assert_called_once_with("t-1")     # la validation a bien tourné
    inst.upsert_rows.assert_not_called()


def test_rows_upsert_refuses_unknown_and_non_input_slugs_before_calling():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_columns.return_value = _COLUMNS
        with pytest.raises(McpError) as e:
            _tool("origami_rows").fn(
                op="upsert", table_id="t-1",
                rows=[{"Email": "a@b.fr", "fit-score": 9}], match_columns=["Email"])
    msg = str(e.value)
    assert "Email" in msg and "fit-score" in msg and "first-name" in msg  # slugs valides nommés
    inst.upsert_rows.assert_not_called()


def test_rows_upsert_real_call_and_enrich_default_false():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_columns.return_value = _COLUMNS
        inst.upsert_rows.return_value = {"object": "enrichment_run", "id": "run-1",
                                         "counts": {"inserted": 1, "updated": 0, "skipped": 0}}
        out = _tool("origami_rows").fn(
            op="upsert", table_id="t-1", rows=[{"email": "a@b.fr"}], match_columns=["email"])
    inst.upsert_rows.assert_called_once_with("t-1", [{"email": "a@b.fr"}], ["email"],
                                             enrich=False)
    assert out["sent"] == 1 and out["receipt"]["counts"]["inserted"] == 1


def test_rows_upsert_local_guards_never_hit_the_client():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_columns.return_value = _COLUMNS
        t = _tool("origami_rows")
        with pytest.raises(McpError):
            t.fn(op="upsert", table_id="t-1", rows=[], match_columns=["email"])
        with pytest.raises(McpError):
            t.fn(op="upsert", table_id="t-1", rows=[{"email": "x"}] * 101, match_columns=["email"])
        with pytest.raises(McpError):
            t.fn(op="upsert", table_id="t-1", rows=[{"email": "x"}])           # match manquant
        with pytest.raises(McpError, match="MISSING_MATCH_VALUE"):
            t.fn(op="upsert", table_id="t-1", rows=[{"email": ""}], match_columns=["email"])
    inst.upsert_rows.assert_not_called()


def test_rows_upsert_degrades_when_columns_unreadable():
    """Colonnes illisibles (forme inattendue) → on n'invente pas de refus : l'API tranche."""
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_columns.return_value = {"weird": True}
        out = _tool("origami_rows").fn(
            op="upsert", table_id="t-1", rows=[{"email": "a"}], match_columns=["email"],
            dry_run=True)
    assert out["check"] == {"columns_available": False}


def test_upstream_unknown_fields_becomes_actionable():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_columns.return_value = {"items": []}     # pas de garde locale
        inst.upsert_rows.side_effect = _upstream(
            400, {"error": "Unknown fields", "code": "UNKNOWN_FIELDS",
                  "details": {"fields": ["Email"]}})
        with pytest.raises(McpError, match="slug"):
            _tool("origami_rows").fn(op="upsert", table_id="t-1", rows=[{"Email": "a"}],
                                     match_columns=["Email"])


# --- upload CSV ---------------------------------------------------------------

_CSV = "email,first-name\na@b.fr,Ana\nc@d.fr,Cléo\n\n"


def test_upload_csv_dry_run_previews_first_rows():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        out = _tool("origami_upload_csv").fn(
            workspace_id="ws-1", filename="leads.csv", csv_text=_CSV, dry_run=True)
    assert out["dry_run"] is True
    assert out["would_upload"]["columns"] == ["email", "first-name"]
    assert out["would_upload"]["rows"] == 2
    assert out["would_upload"]["preview"][0] == {"email": "a@b.fr", "first-name": "Ana"}
    cls.return_value.upload_documents.assert_not_called()


def test_upload_csv_sends_base64_json_table_mode():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        # forme réelle mesurée le 17/08/2026 : la table créée est sous results[0].table
        inst.upload_documents.return_value = {
            "results": [{"kind": "table", "table": {"id": "t-9", "slug": "leads"}}]}
        out = _tool("origami_upload_csv").fn(
            workspace_id="ws-1", filename="leads.csv", csv_text=_CSV)
    ws, files = inst.upload_documents.call_args.args
    assert ws == "ws-1" and len(files) == 1
    assert files[0]["filename"] == "leads.csv" and files[0]["mode"] == "table"
    assert base64.b64decode(files[0]["content"]).decode() == _CSV
    assert "tableId" not in files[0]
    assert out["rows_sent"] == 2 and out["result"]["results"][0]["table"]["id"] == "t-9"
    # l'id de la table créée est remonté au premier niveau : c'est lui qui conditionne
    # l'upsert et la campagne qui suivent (un harnais l'a raté à 3 niveaux de profondeur)
    assert out["table_id"] == "t-9" and out["table_slug"] == "leads"
    assert "error" not in out


def test_upload_csv_surfaces_table_id_from_flat_shape_and_error_kind():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.upload_documents.return_value = {"results": [{"kind": "table", "tableId": "t-flat"}]}
        out = _tool("origami_upload_csv").fn(
            workspace_id="ws-1", filename="leads.csv", csv_text=_CSV)
    assert out["table_id"] == "t-flat" and out["table_slug"] is None

    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.upload_documents.return_value = {
            "results": [{"kind": "error", "filename": "leads.csv", "error": "Unsupported encoding"}]}
        out = _tool("origami_upload_csv").fn(
            workspace_id="ws-1", filename="leads.csv", csv_text=_CSV)
    # un refus par fichier n'est pas un succès muet : il remonte en `error`, sans table_id
    assert out["table_id"] is None
    assert out["error"] == "Unsupported encoding"


def test_upload_csv_append_est_refuse_et_renvoie_vers_upsert():
    """#1349 : Origami ne lie pas l'en-tête aux colonnes de la table, il en crée de
    neuves à chaque appel (8 colonnes devenues 64), en répondant 200. Rien ne part ;
    le refus nomme `origami_rows op=upsert`, qui valide les slugs."""
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        with pytest.raises(McpError) as e:
            _tool("origami_upload_csv").fn(workspace_id="ws-1", filename="l.csv",
                                           csv_text=_CSV, mode="append", table_id="t-1")
        assert "origami_rows(op='upsert'" in str(e.value) and "'t-1'" in str(e.value)
    cls.return_value.upload_documents.assert_not_called()


def test_upload_csv_rejects_non_csv_or_headerless():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        t = _tool("origami_upload_csv")
        with pytest.raises(McpError):
            t.fn(workspace_id="ws-1", filename="leads.xlsx", csv_text=_CSV)
        with pytest.raises(McpError):
            t.fn(workspace_id="ws-1", filename="leads.csv", csv_text="email\n")  # en-tête seul
    cls.return_value.upload_documents.assert_not_called()


# --- campagnes (lecture) ------------------------------------------------------

def test_campaigns_read_ops():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        _tool("origami_campaigns").fn(op="list_for_table", table_id="t-1")
        inst.list_campaigns.assert_called_once_with("t-1")
        _tool("origami_campaigns").fn(op="get", campaign_id="c-1")
        inst.get_campaign.assert_called_once_with("c-1")
        _tool("origami_campaigns").fn(op="stats", campaign_id="c-1")
        inst.campaign_stats.assert_called_once_with("c-1")
        _tool("origami_campaigns").fn(op="people", campaign_id="c-1", cursor="k", status="sent")
        inst.campaign_people.assert_called_once_with("c-1", cursor="k", status="sent", search=None)
        with pytest.raises(McpError):
            _tool("origami_campaigns").fn(op="list_for_table")
        with pytest.raises(McpError):
            _tool("origami_campaigns").fn(op="stats")


# --- campagne : create (agentique) --------------------------------------------

def test_campaign_create_dry_run_then_real_with_settings():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        out = _tool("origami_campaign_create").fn(
            table_id="t-1", instructions="Relance grossistes", dry_run=True)
        assert out["dry_run"] is True
        assert out["would_create"]["settings"] == {"blockPriorContacts": True,
                                                   "blockActiveDuplicates": True}
        inst.create_campaign.assert_not_called()

        inst.create_campaign.return_value = {"agent": {"id": "ag-1"}, "run": {"id": "run-1"},
                                             "table": {"id": "t-1"}}
        out = _tool("origami_campaign_create").fn(
            table_id="t-1", instructions="Relance grossistes",
            block_prior_contacts=False, block_active_duplicates=True)
    inst.create_campaign.assert_called_once_with(
        "t-1", "Relance grossistes",
        settings={"blockPriorContacts": False, "blockActiveDuplicates": True})
    assert out["agent_id"] == "ag-1" and out["run_id"] == "run-1"
    assert "origami_campaign_launch" in out["next"]


def test_campaign_create_guards():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        t = _tool("origami_campaign_create")
        with pytest.raises(McpError):
            t.fn(table_id="t-1", instructions="   ")
        with pytest.raises(McpError):
            t.fn(table_id="t-1", instructions="x" * 10_001)
    cls.return_value.create_campaign.assert_not_called()


def test_run_get_reads_under_the_agent():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        cls.return_value.get_run.return_value = {"id": "run-1", "status": "completed"}
        out = _tool("origami_run_get").fn(agent_id="ag-1", run_id="run-1", include="stats")
    cls.return_value.get_run.assert_called_once_with("ag-1", "run-1", include="stats")
    assert out["status"] == "completed"


# --- campagne : launch (dry_run par défaut) -----------------------------------

_CAMPAIGN = {"id": "c-1", "name": "Grossistes", "status": "draft",
             "channels": {"email": True, "linkedin": False},
             "settings": {"blockPriorContacts": True, "blockActiveDuplicates": True,
                          "autoTopUpEnabled": False},
             "tableId": "t-1", "outOfLeads": False}


def test_launch_default_is_dry_run_and_uses_the_api_preview():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.get_campaign.return_value = _CAMPAIGN
        inst.launch_campaign.return_value = {"dryRun": True, "campaignId": "c-1",
                                             "wouldLaunch": True}
        out = _tool("origami_campaign_launch").fn(campaign_id="c-1")
    assert out["dry_run"] is True
    assert out["campaign"]["status"] == "draft"
    assert out["preview"]["wouldLaunch"] is True
    inst.launch_campaign.assert_called_once_with("c-1", dry_run=True)


def test_launch_real_reports_launched_count():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.get_campaign.return_value = _CAMPAIGN
        inst.launch_campaign.return_value = {
            "object": "campaign", "id": "c-1", "status": "active", "launched": 12,
            "launch": {"scheduled": 12, "firstScheduledAt": "2026-08-18T08:00:00Z",
                       "missingRecipientCount": 0, "duplicateActiveCancelledCount": 1,
                       "duplicatePriorCancelledCount": 2}}
        out = _tool("origami_campaign_launch").fn(campaign_id="c-1", dry_run=False)
    inst.launch_campaign.assert_called_once_with("c-1", dry_run=False)
    assert "dry_run" not in out
    assert out["launched"] == 12
    assert "blocked_missing_channels" not in out


def test_launch_blocked_missing_channels_is_surfaced():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.get_campaign.return_value = _CAMPAIGN
        inst.launch_campaign.return_value = {
            "object": "campaign", "id": "c-1", "status": "active", "launched": 0,
            "launch": {"scheduled": 0, "firstScheduledAt": None, "missingRecipientCount": 0,
                       "duplicateActiveCancelledCount": 0, "duplicatePriorCancelledCount": 0,
                       "blocked": {"reason": "no_sender", "message": "No email account",
                                   "missingChannels": ["email"]}}}
        out = _tool("origami_campaign_launch").fn(campaign_id="c-1", dry_run=False)
    assert out["blocked_missing_channels"] == ["email"]
    assert "Nothing was sent" in out["note"]


# --- pause / resume -----------------------------------------------------------

def test_pause_and_resume_dry_run_wrap_the_api_preview():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.pause_campaign.return_value = {"dryRun": True, "campaignId": "c-1", "wouldPause": True}
        out = _tool("origami_campaign_pause").fn(campaign_id="c-1", dry_run=True)
        assert out == {"dry_run": True, "preview": {"dryRun": True, "campaignId": "c-1",
                                                    "wouldPause": True}}
        inst.pause_campaign.assert_called_once_with("c-1", dry_run=True)

        inst.resume_campaign.return_value = {"id": "c-1", "status": "active", "launched": 3,
                                             "resume": {"resumedSequences": 3,
                                                        "noAccountSequences": 0,
                                                        "missingChannels": []}}
        out = _tool("origami_campaign_resume").fn(campaign_id="c-1")
        assert out["resume"]["resumedSequences"] == 3
        inst.resume_campaign.assert_called_once_with("c-1", dry_run=False)


# --- delete : deux temps + re-GET ---------------------------------------------

def test_delete_without_confirm_is_a_preview():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.delete_campaign.return_value = {"id": "c-1", "name": "G",
                                             "confirmationRequired": True, "status": "draft"}
        out = _tool("origami_campaign_delete").fn(campaign_id="c-1")
    inst.delete_campaign.assert_called_once_with("c-1", confirm=False, dry_run=False)
    assert out["dry_run"] is True and out["deleted"] is False
    assert out["preview"]["confirmationRequired"] is True
    inst.get_campaign.assert_not_called()


def test_delete_confirm_dry_run_forces_the_preview():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.delete_campaign.return_value = {"id": "c-1", "name": "G",
                                             "confirmationRequired": True, "status": "draft"}
        out = _tool("origami_campaign_delete").fn(campaign_id="c-1", confirm=True, dry_run=True)
    inst.delete_campaign.assert_called_once_with("c-1", confirm=True, dry_run=True)
    assert out["dry_run"] is True and out["deleted"] is False


def test_delete_confirm_re_reads_and_requires_404():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.delete_campaign.return_value = {"id": "c-1", "name": "G", "deleted": True}
        inst.get_campaign.side_effect = _upstream(404, {"code": "CAMPAIGN_NOT_FOUND"})
        out = _tool("origami_campaign_delete").fn(campaign_id="c-1", confirm=True)
    inst.delete_campaign.assert_called_once_with("c-1", confirm=True)
    inst.get_campaign.assert_called_once_with("c-1")
    assert out["really_deleted"] is True


def test_delete_confirm_200_but_still_readable_is_reported_as_not_deleted():
    """Le 2e temps peut répondre 200 sans supprimer : seul un 404 au re-GET fait foi."""
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.delete_campaign.return_value = {"id": "c-1", "name": "G", "deleted": True}
        inst.get_campaign.return_value = {"id": "c-1", "name": "G", "status": "draft"}
        out = _tool("origami_campaign_delete").fn(campaign_id="c-1", confirm=True)
    assert out["really_deleted"] is False
    assert out["after"]["status"] == "draft"
    assert "NOT deleted" in out["note"]


def test_delete_re_read_other_error_propagates_as_tool_error():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.delete_campaign.return_value = {"id": "c-1", "deleted": True}
        inst.get_campaign.side_effect = _upstream(503, "down")
        with pytest.raises(McpError, match="unavailable"):
            _tool("origami_campaign_delete").fn(campaign_id="c-1", confirm=True)


# --- sequences ----------------------------------------------------------------

def test_sequences_xor_and_dispatch():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        _tool("origami_sequences").fn(workspace_id="ws-1", status="active", channel="email")
        inst.list_sequences.assert_called_once_with("ws-1", cursor=None, status="active",
                                                    channel="email", recipient=None)
        _tool("origami_sequences").fn(sequence_id="seq-1")
        inst.get_sequence.assert_called_once_with("seq-1")
        with pytest.raises(McpError):
            _tool("origami_sequences").fn()
        with pytest.raises(McpError):
            _tool("origami_sequences").fn(workspace_id="ws-1", sequence_id="seq-1")


def test_sequences_follows_next_cursor_and_lists_distinct_campaigns():
    """Le mode liste PAGINE côté serveur (50/page) et rend `campaign_ids` DISTINCTS.

    Verrouille la régression mesurée le 17/08/2026 sur le workspace réel : une seule
    page rendait 50 séquences / 1 campagne là où il y en avait 369 / 4 — un agent qui
    énumère les campagnes par cette vue concluait à tort qu'il n'en existait qu'une.
    """
    pages = [
        {"items": [{"id": f"s{i}", "campaignId": "camp-A"} for i in range(50)], "nextCursor": "c2"},
        {"items": [{"id": f"s{i}", "campaignId": "camp-B"} for i in range(50, 90)], "nextCursor": "c3"},
        {"items": [{"id": "s90", "campaignId": "camp-A"}], "nextCursor": None},
    ]
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_sequences.side_effect = pages
        out = _tool("origami_sequences").fn(workspace_id="ws-1")
        assert inst.list_sequences.call_count == 3
        assert [c.kwargs["cursor"] for c in inst.list_sequences.call_args_list] == [None, "c2", "c3"]
        assert out["count"] == 91 and out["pages_fetched"] == 3
        assert out["campaign_ids"] == ["camp-A", "camp-B"]
        assert out["truncated"] is False and out["cursor"] is None

    # max_pages borne la lecture et le signale : cursor rendu, truncated=True
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        inst = cls.return_value
        inst.list_sequences.side_effect = pages
        out = _tool("origami_sequences").fn(workspace_id="ws-1", max_pages=1)
        assert inst.list_sequences.call_count == 1
        assert out["count"] == 50 and out["truncated"] is True and out["cursor"] == "c2"
        with pytest.raises(McpError):
            _tool("origami_sequences").fn(workspace_id="ws-1", max_pages=0)


# --- erreurs amont ------------------------------------------------------------

def test_upstream_401_becomes_an_actionable_tool_error():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        cls.return_value.list_tables.side_effect = _upstream(
            401, {"error": "Unauthorized", "code": "UNAUTHORIZED"})
        with pytest.raises(McpError, match="og_live_"):
            _tool("origami_tables").fn()


def test_upstream_402_names_credits():
    with patch("oto.tools.origami.client.OrigamiClient") as cls:
        cls.return_value.create_campaign.side_effect = _upstream(
            402, {"error": "Not enough credits", "code": "INSUFFICIENT_CREDITS"})
        with pytest.raises(McpError, match="INSUFFICIENT_CREDITS"):
            _tool("origami_campaign_create").fn(table_id="t-1", instructions="x")
