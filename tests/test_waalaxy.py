"""Connecteur Waalaxy — prospection LinkedIn, API publique import-only
(developers.waalaxy.com).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection), la
doc how-to, la surface MCP (3 tools avec description — régression du piège
f-string-docstring), la sonde « tester la connexion », la jointure
tool↔client oto-core, l'exclusivité prospect/prospects, le dry_run, et le
reçu qui lit les codes par item (Waalaxy répond 200 même en échec).
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import waalaxy

EXPECTED_TOOLS = {"waalaxy_prospect_list", "waalaxy_campaign", "waalaxy_prospect"}
URL = "https://www.linkedin.com/in/jane-doe"


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
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


def _fn_with_mock_client():
    """`WaalaxyClient` mocké sauf `build_add_prospects_body` (pure, gardée réelle
    pour que dry_run exerce les vraies gardes)."""
    from fastmcp import FastMCP
    from oto.tools.waalaxy.client import WaalaxyClient as real

    patcher = patch("oto.tools.waalaxy.client.WaalaxyClient")
    cls = patcher.start()
    cls.build_add_prospects_body = staticmethod(real.build_add_prospects_body)
    m = FastMCP("t")
    waalaxy.register(m)
    return m, cls, patcher


def test_waalaxy_is_keyed_byo_only_connector():
    c = providers.REGISTRY["waalaxy"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert c.default_active is False
    assert "waalaxy" in providers.KEY_PROVIDERS
    assert c.category == "Outreach"
    assert c.publisher_name == "Waalaxy"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["waalaxy"] == "waalaxy.com"
    assert [f.name for f in c.credential_fields] == ["key"]


def test_waalaxy_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["waalaxy"].doc_sections}
    assert {"prerequisite", "usage", "note"} <= kinds


def test_tools_register_under_namespace_with_descriptions(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    for name in EXPECTED_TOOLS:
        assert namespace_of(name) == "waalaxy"
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered_and_checks_body():
    m, cls, patcher = _fn_with_mock_client()
    try:
        assert connector_verify.supports("waalaxy")
        cls.return_value.test_connection.return_value = True
        waalaxy._verify({"key": "k"})
        cls.return_value.test_connection.return_value = {"html": "…"}
        with pytest.raises(RuntimeError, match="did not answer true"):
            waalaxy._verify({"key": "k"})
    finally:
        patcher.stop()
    import oto.tools.waalaxy.client as real
    assert not isinstance(real.WaalaxyClient, type(cls)), "patch leaked"


def test_client_exposes_methods_called_by_tools():
    from oto.tools.waalaxy.client import WaalaxyClient
    for meth in ("test_connection", "list_prospect_lists", "list_campaigns", "add_prospects",
                 "build_add_prospects_body"):
        assert callable(getattr(WaalaxyClient, meth, None)), f"WaalaxyClient.{meth} manquant"


def test_list_tools_delegate():
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.list_prospect_lists.return_value = [{"_id": "l1"}]
        assert asyncio.run(m.get_tool("waalaxy_prospect_list")).fn() == [{"_id": "l1"}]
        cls.return_value.list_campaigns.return_value = {"total": 0, "campaigns": []}
        assert asyncio.run(m.get_tool("waalaxy_campaign")).fn()["total"] == 0
    finally:
        patcher.stop()


def test_add_requires_exactly_one_of_prospect_prospects_and_list_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("waalaxy_prospect")).fn
        with pytest.raises(McpError, match="exactly one"):
            fn(prospect_list_id="l1")
        with pytest.raises(McpError, match="exactly one"):
            fn(prospect_list_id="l1", prospect={"url": URL}, prospects=[{"url": URL}])
        with pytest.raises(McpError, match="prospect_list_id"):
            fn(prospect={"url": URL})
        with pytest.raises(McpError, match="url"):
            fn(prospect_list_id="l1", prospect={"customProfile": {}})
        with pytest.raises(McpError, match="LinkedIn"):
            fn(prospect_list_id="l1", prospect={"url": "https://acme.com"})
        with pytest.raises(McpError, match="max 100"):
            fn(prospect_list_id="l1", prospects=[{"url": URL}] * 101)
        cls.return_value.add_prospects.assert_not_called()
    finally:
        patcher.stop()


def test_add_dry_run_returns_payload_without_calling():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("waalaxy_prospect")).fn
        out = fn(prospect_list_id="l1", campaign_id="c1", prospect={"url": URL},
                 move_duplicates_to_other_list=True, dry_run=True)
        assert out["dry_run"] is True and out["total"] == 1
        assert out["would_post"]["campaignId"] == "c1"
        assert out["would_post"]["moveDuplicatesToOtherList"] is True
        assert "canCreateDuplicates" not in out["would_post"]
        cls.return_value.add_prospects.assert_not_called()
        with pytest.raises(McpError, match="1000"):
            fn(prospect_list_id="l1", dry_run=True,
               prospect={"url": URL, "customVariables": [{"label": "a", "value": "x" * 1001}]})
        with pytest.raises(McpError, match="origin"):
            fn(prospect_list_id="l1", prospect={"url": URL}, origin="", dry_run=True)
    finally:
        patcher.stop()


def test_add_receipt_reads_per_item_codes():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("waalaxy_prospect")).fn
        cls.return_value.add_prospects.return_value = {"result": [
            {"importCode": "success", "addToCampaignCode": "success",
             "prospect": {"_id": "p1", "profile": {}}},
            {"importCode": "duplicated_prospect", "message": "already exists"},
            {"importCode": "success", "addToCampaignCode": "already_in_campaign"},
        ]}
        out = fn(prospect_list_id="l1", campaign_id="c1",
                 prospects=[{"url": URL}, {"url": URL + "2"}, {"url": URL + "3"}])
        assert (out["total"], out["imported"], out["enrolled"]) == (3, 2, 1)
        assert [f["code"] for f in out["failed"]] == ["duplicated_prospect", "already_in_campaign"]
        assert out["failed"][0]["url"] == URL + "2"
        assert out["failed"][1]["stage"] == "campaign"
        assert "result" not in out
        assert out["items"][0] == {"index": 0, "url": URL, "importCode": "success",
                                   "addToCampaignCode": "success", "prospect_id": "p1",
                                   "publicIdentifier": None}
        cls.return_value.add_prospects.assert_called_once()
        args, kwargs = cls.return_value.add_prospects.call_args
        assert args[1] == "l1" and kwargs["campaign_id"] == "c1" and kwargs["origin"] == "oto"
    finally:
        patcher.stop()


def test_add_single_prospect_receipt_without_campaign():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("waalaxy_prospect")).fn
        cls.return_value.add_prospects.return_value = {"result": [{"importCode": "max_limit_crm"}]}
        out = fn(prospect_list_id="l1", prospect={"url": URL})
        assert out["imported"] == 0 and "enrolled" not in out
        assert "addToCampaignCode" not in out["items"][0]
        assert out["failed"][0]["code"] == "max_limit_crm"
    finally:
        patcher.stop()


def test_upstream_401_is_a_clear_key_error():
    from oto.tools.common.errors import UpstreamHTTPError
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.list_campaigns.side_effect = UpstreamHTTPError(401, {"title": "Unauthorized"})
        with pytest.raises(McpError, match="rejected the key"):
            asyncio.run(m.get_tool("waalaxy_campaign")).fn()
    finally:
        patcher.stop()


def test_add_receipt_edge_cases():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("waalaxy_prospect")).fn
        # importé mais sans addToCampaignCode alors qu'une campagne était demandée → signalé
        cls.return_value.add_prospects.return_value = {"result": [{"importCode": "success"}]}
        out = fn(prospect_list_id="l1", campaign_id="c1", prospect={"url": URL})
        assert out["enrolled"] == 0 and out["failed"][0]["code"] == "not_enrolled"
        # réponse plus courte que le lot → total reste 3, manquants signalés
        out = fn(prospect_list_id="l1", prospects=[{"url": URL}, {"url": URL + "2"}, {"url": URL + "3"}])
        assert out["total"] == 3 and out["imported"] == 1 and "warning" in out
        assert [f["code"] for f in out["failed"]] == ["no_result", "no_result"]
        # corps inattendu → forme complète, `failed` lisible
        cls.return_value.add_prospects.return_value = True
        out = fn(prospect_list_id="l1", prospect={"url": URL})
        assert out["imported"] == 0 and out["failed"][0]["code"] == "unexpected_response"
    finally:
        patcher.stop()
