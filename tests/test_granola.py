"""Connecteur Granola — notes de réunion/transcripts/dossiers/webhooks
(public-api.granola.ai).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Knowledge), la
doc how-to, la surface MCP (2 tools — `granola_content` (list_notes/get_note/
get_transcript/list_folders) et `granola_webhook_endpoint` (CRUD) — chacun
avec une description, régression du piège f-string-docstring), la sonde
« tester la connexion », la jointure tool↔client oto-core (garde
version-skew), et le dispatch `op=` (required manquant refusé, arg non
pertinent pour CET op refusé).
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import granola

EXPECTED_TOOLS = {"granola_content", "granola_webhook_endpoint"}


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
    """Enregistre le module avec `GranolaClient` mocké, DANS le patch (sinon
    `register()`'s `from ... import GranolaClient` capture la vraie classe
    avant que le patch ne s'applique)."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.granola.client.GranolaClient")
    cls = patcher.start()
    m = FastMCP("t")
    granola.register(m)
    return m, cls, patcher


# --- registre -----------------------------------------------------------------

def test_granola_is_keyed_byo_only_connector():
    c = providers.REGISTRY["granola"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert "granola" in providers.KEY_PROVIDERS
    assert c.category == "Calls & meetings"
    assert c.publisher_name == "Granola"
    assert c.label == "Granola"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["granola"] == "granola.ai"


def test_granola_registered_among_keyed_connectors():
    """L'ordre des `keyed` est chargé (`status_for` en dépend) : on n'insère
    jamais au milieu. On vérifie l'APPARTENANCE, pas la position — deux
    connecteurs fusionnés le même jour ne peuvent pas être « le dernier »
    tous les deux (vécu le 21/08/2026 : main au rouge sur ce motif)."""
    keyed_names = [c.name for c in providers._REGISTRY_LIST if c.keyed]
    assert "granola" in keyed_names


def test_granola_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["granola"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------------

def test_granola_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "granola" for t in all_tools if t.startswith("granola_"))


def test_granola_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_audit_endpoint_not_exposed_anywhere(all_tools):
    """Live-tested 404 on a real key — dropped from both the client and the
    tool surface rather than shipping a call nothing can currently use."""
    assert "granola_audit" not in all_tools
    from oto.tools.granola.client import GranolaClient
    assert not hasattr(GranolaClient, "list_audit_events")


def test_verify_probe_registered():
    _fn_with_mock_client()
    assert connector_verify.supports("granola")


# --- jointure tool ↔ client oto-core (garde version-skew) -------------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.granola.client import GranolaClient
    for meth in ("list_notes", "get_note", "get_transcript", "list_folders",
                 "list_webhook_endpoints", "create_webhook_endpoint",
                 "update_webhook_endpoint", "delete_webhook_endpoint"):
        assert callable(getattr(GranolaClient, meth, None)), f"GranolaClient.{meth} manquant"


# --- dispatch op= (comportement) ---------------------------------------------------

def test_content_list_notes_refuses_get_only_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_content")).fn
        with pytest.raises(McpError, match="op='list_notes' does not use"):
            fn(op="list_notes", note_id="not_abc")
        cls.return_value.list_notes.assert_not_called()
    finally:
        patcher.stop()


def test_content_get_note_requires_note_id_and_refuses_list_filters():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_content")).fn
        with pytest.raises(McpError, match="requires .note_id."):
            fn(op="get_note")
        with pytest.raises(McpError, match="op='get_note' does not use"):
            fn(op="get_note", note_id="not_abc", folder_id="fol_x")
        cls.return_value.get_note.assert_not_called()

        cls.return_value.get_note.return_value = {"id": "not_abc"}
        result = fn(op="get_note", note_id="not_abc", include="transcript")
        assert result == {"id": "not_abc"}
        cls.return_value.get_note.assert_called_once_with("not_abc", include="transcript")
    finally:
        patcher.stop()


def test_content_list_notes_passes_filters():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_content")).fn
        cls.return_value.list_notes.return_value = {"notes": [], "hasMore": False}
        fn(op="list_notes", folder_id="fol_x", page_size=5)
        cls.return_value.list_notes.assert_called_once_with(folder_id="fol_x", page_size=5)
    finally:
        patcher.stop()


def test_content_get_transcript_requires_note_id_and_refuses_note_only_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_content")).fn
        with pytest.raises(McpError, match="requires .note_id."):
            fn(op="get_transcript")
        with pytest.raises(McpError, match="op='get_transcript' does not use"):
            fn(op="get_transcript", note_id="not_abc", include="transcript")

        cls.return_value.get_transcript.return_value = {}
        fn(op="get_transcript", note_id="not_abc", page_size=100)
        cls.return_value.get_transcript.assert_called_once_with("not_abc", page_size=100)
    finally:
        patcher.stop()


def test_content_list_folders_refuses_note_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_content")).fn
        with pytest.raises(McpError, match="op='list_folders' does not use"):
            fn(op="list_folders", note_id="not_abc")

        cls.return_value.list_folders.return_value = {}
        fn(op="list_folders", page_size=30)
        cls.return_value.list_folders.assert_called_once_with(page_size=30)
    finally:
        patcher.stop()


def test_webhook_endpoint_create_requires_url_and_scopes():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_webhook_endpoint")).fn
        with pytest.raises(McpError, match="requires .url. and .scopes."):
            fn(op="create")

        cls.return_value.create_webhook_endpoint.return_value = {"id": "whe_x"}
        fn(op="create", url="https://example.com/hook", scopes=["personal"])
        cls.return_value.create_webhook_endpoint.assert_called_once_with(
            "https://example.com/hook", ["personal"])
    finally:
        patcher.stop()


def test_webhook_endpoint_update_delete_require_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_webhook_endpoint")).fn
        with pytest.raises(McpError, match="requires .webhook_endpoint_id."):
            fn(op="update", enabled=False)
        with pytest.raises(McpError, match="requires .webhook_endpoint_id."):
            fn(op="delete")

        cls.return_value.delete_webhook_endpoint.return_value = {"deleted": True}
        fn(op="delete", webhook_endpoint_id="whe_x")
        cls.return_value.delete_webhook_endpoint.assert_called_once_with("whe_x")

        cls.return_value.update_webhook_endpoint.return_value = {}
        fn(op="update", webhook_endpoint_id="whe_x", enabled=False)
        cls.return_value.update_webhook_endpoint.assert_called_once_with("whe_x", enabled=False)
    finally:
        patcher.stop()


def test_webhook_endpoint_update_requires_at_least_one_field():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_webhook_endpoint")).fn
        with pytest.raises(McpError, match="at least one field"):
            fn(op="update", webhook_endpoint_id="whe_x")
    finally:
        patcher.stop()


def test_webhook_endpoint_delete_refuses_extra_fields():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("granola_webhook_endpoint")).fn
        with pytest.raises(McpError, match="op='delete' does not use"):
            fn(op="delete", webhook_endpoint_id="whe_x", enabled=True)
        cls.return_value.delete_webhook_endpoint.assert_not_called()
    finally:
        patcher.stop()
