"""Connecteur Grain — enregistrements de réunion/transcripts/partage/webhooks
(api.grain.com).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Knowledge), la
doc how-to, la surface MCP (5 tools — `grain_recording`, `grain_transcript`,
`grain_recording_file`, `grain_hook`, `grain_org` — chacun avec une
description, régression du piège f-string-docstring), la sonde « tester la
connexion », la jointure tool↔client oto-core (garde version-skew), et le
dispatch `op=` (required manquant refusé, arg non pertinent pour CET op
refusé).
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import grain

EXPECTED_TOOLS = {
    "grain_recording", "grain_transcript", "grain_recording_file",
    "grain_hook", "grain_org",
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
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


def _fn_with_mock_client():
    """Enregistre le module avec `GrainClient` mocké, DANS le patch (sinon
    `register()`'s `from ... import GrainClient` capture la vraie classe
    avant que le patch ne s'applique)."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.grain.client.GrainClient")
    cls = patcher.start()
    m = FastMCP("t")
    grain.register(m)
    return m, cls, patcher


# --- registre -----------------------------------------------------------------

def test_grain_is_keyed_byo_only_connector():
    c = providers.REGISTRY["grain"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert "grain" in providers.KEY_PROVIDERS
    assert c.category == "Calls & meetings"
    assert c.publisher_name == "Grain"
    assert c.label == "Grain"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["grain"] == "grain.com"


def test_grain_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["grain"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------------

def test_grain_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "grain" for t in all_tools if t.startswith("grain_"))


def test_grain_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    _fn_with_mock_client()
    assert connector_verify.supports("grain")


# --- jointure tool ↔ client oto-core (garde version-skew) -------------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.grain.client import GrainClient
    for meth in ("list_recordings", "get_recording", "update_recording", "add_tag",
                 "remove_tag", "share_with_user", "unshare_from_user", "share_with_team",
                 "unshare_from_team", "get_transcript", "get_transcript_text",
                 "get_transcript_vtt", "get_transcript_srt", "download_recording",
                 "create_upload_url", "list_hooks", "create_hook", "delete_hook",
                 "list_users", "list_teams", "list_meeting_types"):
        assert callable(getattr(GrainClient, meth, None)), f"GrainClient.{meth} manquant"


# --- dispatch op= (comportement) ---------------------------------------------------

def test_recording_list_refuses_recording_only_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_recording")).fn
        with pytest.raises(McpError, match="op='list' does not use"):
            fn(op="list", recording_id="rec_1")
        cls.return_value.list_recordings.assert_not_called()
    finally:
        patcher.stop()


def test_recording_get_requires_id_and_refuses_list_filters():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_recording")).fn
        with pytest.raises(McpError, match="requires .recording_id."):
            fn(op="get")
        with pytest.raises(McpError, match="op='get' does not use"):
            fn(op="get", recording_id="rec_1", filter={"team": "t1"})

        cls.return_value.get_recording.return_value = {"id": "rec_1"}
        result = fn(op="get", recording_id="rec_1", include={"highlights": True})
        assert result == {"id": "rec_1"}
        cls.return_value.get_recording.assert_called_once_with("rec_1", include={"highlights": True})
    finally:
        patcher.stop()


def test_recording_update_requires_title():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_recording")).fn
        with pytest.raises(McpError, match="requires .title."):
            fn(op="update", recording_id="rec_1")

        cls.return_value.update_recording.return_value = {}
        fn(op="update", recording_id="rec_1", title="New title")
        cls.return_value.update_recording.assert_called_once_with("rec_1", "New title")
    finally:
        patcher.stop()


def test_recording_tag_untag():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_recording")).fn
        with pytest.raises(McpError, match="requires .tag."):
            fn(op="tag", recording_id="rec_1")

        cls.return_value.add_tag.return_value = {}
        fn(op="tag", recording_id="rec_1", tag="important")
        cls.return_value.add_tag.assert_called_once_with("rec_1", "important")

        cls.return_value.remove_tag.return_value = {}
        fn(op="untag", recording_id="rec_1", tag="important")
        cls.return_value.remove_tag.assert_called_once_with("rec_1", "important")
    finally:
        patcher.stop()


def test_recording_share_user_and_team():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_recording")).fn
        with pytest.raises(McpError, match="requires .user_id."):
            fn(op="share_user", recording_id="rec_1")
        with pytest.raises(McpError, match="requires .team_id."):
            fn(op="share_team", recording_id="rec_1")

        cls.return_value.share_with_user.return_value = {}
        fn(op="share_user", recording_id="rec_1", user_id="usr_1")
        cls.return_value.share_with_user.assert_called_once_with("rec_1", "usr_1")

        cls.return_value.share_with_team.return_value = {}
        fn(op="share_team", recording_id="rec_1", team_id="team_1")
        cls.return_value.share_with_team.assert_called_once_with("rec_1", "team_1")
    finally:
        patcher.stop()


def test_transcript_formats_dispatch_to_the_right_client_method():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_transcript")).fn

        cls.return_value.get_transcript.return_value = {"text": []}
        fn(recording_id="rec_1", format="json")
        cls.return_value.get_transcript.assert_called_once_with("rec_1")

        cls.return_value.get_transcript_text.return_value = "hello"
        fn(recording_id="rec_1", format="txt")
        cls.return_value.get_transcript_text.assert_called_once_with("rec_1")

        cls.return_value.get_transcript_vtt.return_value = "WEBVTT"
        fn(recording_id="rec_1", format="vtt")
        cls.return_value.get_transcript_vtt.assert_called_once_with("rec_1")

        cls.return_value.get_transcript_srt.return_value = "1\n00:00:00"
        fn(recording_id="rec_1", format="srt")
        cls.return_value.get_transcript_srt.assert_called_once_with("rec_1")
    finally:
        patcher.stop()


def test_recording_file_download_requires_id_and_refuses_upload_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_recording_file")).fn
        with pytest.raises(McpError, match="requires .recording_id."):
            fn(op="download")
        with pytest.raises(McpError, match="op='download' does not use"):
            fn(op="download", recording_id="rec_1", filename="x.mp4")

        cls.return_value.download_recording.return_value = b"\x00\x01\x02"
        result = fn(op="download", recording_id="rec_1")
        assert result["size_bytes"] == 3
    finally:
        patcher.stop()


def test_recording_file_create_upload_url_requires_filename():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_recording_file")).fn
        with pytest.raises(McpError, match="requires .filename."):
            fn(op="create_upload_url")
        with pytest.raises(McpError, match="op='create_upload_url' does not use"):
            fn(op="create_upload_url", filename="x.mp4", recording_id="rec_1")

        cls.return_value.create_upload_url.return_value = {"url": "https://...", "uuid": "u1"}
        fn(op="create_upload_url", filename="x.mp4", user_id="usr_1")
        cls.return_value.create_upload_url.assert_called_once_with("x.mp4", user_id="usr_1")
    finally:
        patcher.stop()


def test_hook_create_requires_url_and_type():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_hook")).fn
        with pytest.raises(McpError, match="requires .hook_url. and .hook_type."):
            fn(op="create")

        cls.return_value.create_hook.return_value = {"id": "hook_1"}
        fn(op="create", hook_url="https://example.com/hook", hook_type="recording_added")
        cls.return_value.create_hook.assert_called_once_with(
            "https://example.com/hook", "recording_added")
    finally:
        patcher.stop()


def test_hook_delete_requires_id_and_refuses_create_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_hook")).fn
        with pytest.raises(McpError, match="requires .hook_id."):
            fn(op="delete")
        with pytest.raises(McpError, match="op='delete' does not use"):
            fn(op="delete", hook_id="hook_1", hook_url="https://example.com")

        cls.return_value.delete_hook.return_value = {"deleted": True}
        fn(op="delete", hook_id="hook_1")
        cls.return_value.delete_hook.assert_called_once_with("hook_1")
    finally:
        patcher.stop()


def test_org_dispatches_by_op():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("grain_org")).fn

        cls.return_value.list_users.return_value = {"users": []}
        fn(op="users")
        cls.return_value.list_users.assert_called_once()

        cls.return_value.list_teams.return_value = {"teams": []}
        fn(op="teams")
        cls.return_value.list_teams.assert_called_once()

        cls.return_value.list_meeting_types.return_value = {"meeting_types": []}
        fn(op="meeting_types")
        cls.return_value.list_meeting_types.assert_called_once()
    finally:
        patcher.stop()
