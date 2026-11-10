"""Connecteur Fireflies — transcripts, réunion en direct, AskFred, org
(api.fireflies.ai/graphql).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Knowledge), la
doc how-to, la surface MCP (7 tools, chacun avec une description — régression
du piège f-string-docstring), la sonde « tester la connexion », la jointure
tool↔client oto-core (garde version-skew), et le dispatch `op=` (required
manquant refusé, arg non pertinent pour CET op refusé).
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import fireflies

EXPECTED_TOOLS = {
    "fireflies_transcript", "fireflies_live_meeting", "fireflies_askfred",
    "fireflies_user", "fireflies_channel", "fireflies_bite", "fireflies_org",
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
    """Enregistre le module avec `FirefliesClient` mocké, DANS le patch
    (sinon `register()`'s `from ... import FirefliesClient` capture la vraie
    classe avant que le patch ne s'applique)."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.fireflies.client.FirefliesClient")
    cls = patcher.start()
    m = FastMCP("t")
    fireflies.register(m)
    return m, cls, patcher


# --- registre -----------------------------------------------------------------

def test_fireflies_is_keyed_byo_only_connector():
    c = providers.REGISTRY["fireflies"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert "fireflies" in providers.KEY_PROVIDERS
    assert c.category == "Calls & meetings"
    assert c.publisher_name == "Fireflies.ai"
    assert c.label == "Fireflies"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["fireflies"] == "fireflies.ai"


def test_fireflies_registered_among_keyed_connectors():
    keyed_names = [c.name for c in providers._REGISTRY_LIST if c.keyed]
    assert "fireflies" in keyed_names


def test_fireflies_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["fireflies"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------------

def test_fireflies_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "fireflies" for t in all_tools if t.startswith("fireflies_"))


def test_fireflies_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    _fn_with_mock_client()
    assert connector_verify.supports("fireflies")


# --- jointure tool ↔ client oto-core (garde version-skew) -------------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.fireflies.client import FirefliesClient
    for meth in (
        "list_transcripts", "get_transcript", "delete_transcript", "upload_audio",
        "update_meeting_title", "update_meeting_privacy", "update_meeting_channel",
        "share_meeting", "revoke_shared_meeting_access", "list_active_meetings",
        "add_to_live_meeting", "list_live_action_items", "create_live_action_item",
        "create_live_soundbite", "update_meeting_state", "list_askfred_threads",
        "get_askfred_thread", "create_askfred_thread", "continue_askfred_thread",
        "delete_askfred_thread", "get_user", "list_users", "list_user_groups",
        "set_user_role", "get_channel", "list_channels", "get_bite", "list_bites",
        "create_bite", "list_contacts", "get_analytics", "list_apps",
        "list_rule_executions_by_meeting", "list_audit_events",
        "create_upload_url", "confirm_upload",
    ):
        assert callable(getattr(FirefliesClient, meth, None)), f"FirefliesClient.{meth} manquant"


# --- dispatch op= (comportement) : fireflies_transcript ----------------------------

def test_transcript_list_refuses_non_list_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn
        with pytest.raises(McpError, match="op='list' does not use"):
            fn(op="list", transcript_id="t1")
        cls.return_value.list_transcripts.assert_not_called()

        cls.return_value.list_transcripts.return_value = []
        fn(op="list", keyword="budget", mine=True)
        cls.return_value.list_transcripts.assert_called_once_with(keyword="budget", mine=True)
    finally:
        patcher.stop()


def test_transcript_get_requires_id_and_refuses_list_filters():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn
        with pytest.raises(McpError, match="op='get' requires"):
            fn(op="get")
        with pytest.raises(McpError, match="op='get' does not use"):
            fn(op="get", transcript_id="t1", limit=10)

        cls.return_value.get_transcript.return_value = {"id": "t1"}
        result = fn(op="get", transcript_id="t1")
        assert result == {"id": "t1"}
        cls.return_value.get_transcript.assert_called_once_with("t1")
    finally:
        patcher.stop()


def test_transcript_delete_requires_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn
        with pytest.raises(McpError, match="op='delete' requires"):
            fn(op="delete")

        cls.return_value.delete_transcript.return_value = {}
        fn(op="delete", transcript_id="t1")
        cls.return_value.delete_transcript.assert_called_once_with("t1")
    finally:
        patcher.stop()


def test_transcript_upload_requires_url_and_refuses_existing_id_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn
        with pytest.raises(McpError, match="op='upload' requires .url."):
            fn(op="upload")
        with pytest.raises(McpError, match="op='upload' does not use"):
            fn(op="upload", url="https://x.com/a.mp3", transcript_id="t1")

        cls.return_value.upload_audio.return_value = {"success": True}
        fn(op="upload", url="https://x.com/a.mp3", title="Call", save_video=True)
        cls.return_value.upload_audio.assert_called_once_with(
            "https://x.com/a.mp3", title="Call", save_video=True)
    finally:
        patcher.stop()


def test_transcript_update_title_privacy_channel():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn

        with pytest.raises(McpError, match="op='update_title' requires"):
            fn(op="update_title", transcript_id="t1")
        cls.return_value.update_meeting_title.return_value = {}
        fn(op="update_title", transcript_id="t1", title="New")
        cls.return_value.update_meeting_title.assert_called_once_with("t1", "New")

        with pytest.raises(McpError, match="op='update_privacy' requires"):
            fn(op="update_privacy", transcript_id="t1")
        cls.return_value.update_meeting_privacy.return_value = {}
        fn(op="update_privacy", transcript_id="t1", privacy="teammates")
        cls.return_value.update_meeting_privacy.assert_called_once_with("t1", "teammates")

        with pytest.raises(McpError, match="op='update_channel' requires"):
            fn(op="update_channel", transcript_ids=["t1"])

        cls.return_value.list_channels.return_value = [{"id": "c1"}]
        cls.return_value.update_meeting_channel.return_value = []
        fn(op="update_channel", transcript_ids=["t1", "t2"], channel_id="c1")
        cls.return_value.update_meeting_channel.assert_called_once_with(["t1", "t2"], "c1")
    finally:
        patcher.stop()


def test_transcript_update_channel_refuses_a_channel_id_fireflies_would_not_validate():
    """Fireflies itself does not validate `channel_id` on `updateMeetingChannel`
    (live-confirmed: a typo silently sticks, with no way to unset it after).
    This tool checks against `list_channels()` first so the agent gets a
    refusal instead of silently corrupting a real transcript's channel."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn
        cls.return_value.list_channels.return_value = [{"id": "c1"}]
        with pytest.raises(McpError, match="not found or inaccessible"):
            fn(op="update_channel", transcript_ids=["t1"], channel_id="typo-channel")
        cls.return_value.update_meeting_channel.assert_not_called()
    finally:
        patcher.stop()


def test_transcript_upload_refuses_list_only_search_params():
    """Regression: `_refuse_ignored` for search params used to be skipped
    entirely on op='upload' (a conditional expression ending
    `if op != "upload" else None`), so `keyword`/`scope`/etc were silently
    dropped instead of refused — contradicting the module's own "no param is
    ever silently dropped" guide."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn
        with pytest.raises(McpError, match="does not use"):
            fn(op="upload", url="https://x.com/a.mp3", keyword="foo")
        cls.return_value.upload_audio.assert_not_called()
    finally:
        patcher.stop()


def test_transcript_list_scope_requires_keyword():
    """Fireflies rejects `scope` passed without `keyword` (`invalid_arguments`)
    — refuse it at the tool layer instead of spending a round-trip on an
    error the API would reject anyway."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn
        with pytest.raises(McpError, match="scope.*requires.*keyword"):
            fn(op="list", scope="title")
        cls.return_value.list_transcripts.assert_not_called()

        cls.return_value.list_transcripts.return_value = []
        fn(op="list", keyword="budget", scope="sentences")
        cls.return_value.list_transcripts.assert_called_once_with(keyword="budget", scope="sentences")
    finally:
        patcher.stop()


def test_transcript_create_and_confirm_upload_url():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn

        with pytest.raises(McpError, match="op='create_upload_url' requires"):
            fn(op="create_upload_url")
        cls.return_value.create_upload_url.return_value = {
            "upload_url": "https://s3.example.com/x", "meeting_id": "m1", "expires_at": "..."}
        result = fn(op="create_upload_url", content_type="audio/mpeg", file_size=1024, title="Call")
        assert result["meeting_id"] == "m1"
        cls.return_value.create_upload_url.assert_called_once_with("audio/mpeg", 1024, title="Call")

        with pytest.raises(McpError, match="op='confirm_upload' requires"):
            fn(op="confirm_upload")
        cls.return_value.confirm_upload.return_value = {"success": True, "meeting_id": "m1", "message": "ok"}
        fn(op="confirm_upload", meeting_id="m1")
        cls.return_value.confirm_upload.assert_called_once_with("m1")
    finally:
        patcher.stop()


def test_transcript_share_and_unshare():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn

        with pytest.raises(McpError, match="op='share' requires"):
            fn(op="share", transcript_id="t1")
        cls.return_value.share_meeting.return_value = {"success": True}
        fn(op="share", transcript_id="t1", emails=["a@x.com"], expiry_days=7)
        cls.return_value.share_meeting.assert_called_once_with("t1", ["a@x.com"], expiry_days=7)

        with pytest.raises(McpError, match="op='unshare' requires"):
            fn(op="unshare", transcript_id="t1")
        cls.return_value.revoke_shared_meeting_access.return_value = {"success": True}
        fn(op="unshare", transcript_id="t1", email="a@x.com")
        cls.return_value.revoke_shared_meeting_access.assert_called_once_with("t1", "a@x.com")
    finally:
        patcher.stop()


# --- dispatch op= : fireflies_live_meeting -----------------------------------------

def test_live_meeting_list_active_and_add_bot():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_live_meeting")).fn

        cls.return_value.list_active_meetings.return_value = []
        fn(op="list_active", email="a@x.com", states=["active"])
        cls.return_value.list_active_meetings.assert_called_once_with(
            email="a@x.com", states=["active"])

        with pytest.raises(McpError, match="op='add_bot' requires"):
            fn(op="add_bot")
        cls.return_value.add_to_live_meeting.return_value = {"success": True}
        fn(op="add_bot", meeting_link="https://zoom.us/j/1", duration=30)
        cls.return_value.add_to_live_meeting.assert_called_once_with(
            "https://zoom.us/j/1", duration=30)
    finally:
        patcher.stop()


def test_live_meeting_action_items_and_state_require_meeting_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_live_meeting")).fn
        with pytest.raises(McpError, match="requires .meeting_id."):
            fn(op="list_action_items")

        cls.return_value.list_live_action_items.return_value = []
        fn(op="list_action_items", meeting_id="m1")
        cls.return_value.list_live_action_items.assert_called_once_with("m1")

        with pytest.raises(McpError, match="requires .prompt."):
            fn(op="create_action_item", meeting_id="m1")
        cls.return_value.create_live_action_item.return_value = {"success": True}
        fn(op="create_action_item", meeting_id="m1", prompt="Follow up")
        cls.return_value.create_live_action_item.assert_called_once_with("m1", "Follow up")

        with pytest.raises(McpError, match="requires .action."):
            fn(op="update_state", meeting_id="m1")
        cls.return_value.update_meeting_state.return_value = {"success": True, "action": "pause_recording"}
        fn(op="update_state", meeting_id="m1", action="pause_recording")
        cls.return_value.update_meeting_state.assert_called_once_with("m1", "pause_recording")
    finally:
        patcher.stop()


# --- dispatch op= : fireflies_askfred ----------------------------------------------

def test_askfred_thread_lifecycle():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_askfred")).fn

        cls.return_value.list_askfred_threads.return_value = []
        fn(op="list_threads", transcript_id="t1")
        cls.return_value.list_askfred_threads.assert_called_once_with(transcript_id="t1")

        with pytest.raises(McpError, match="op='get_thread' requires"):
            fn(op="get_thread")
        cls.return_value.get_askfred_thread.return_value = {"id": "th1"}
        fn(op="get_thread", thread_id="th1")
        cls.return_value.get_askfred_thread.assert_called_once_with("th1")

        with pytest.raises(McpError, match="op='create_thread' requires"):
            fn(op="create_thread")
        cls.return_value.create_askfred_thread.return_value = {"id": "msg1"}
        fn(op="create_thread", query="What were the decisions?", transcript_id="t1")
        cls.return_value.create_askfred_thread.assert_called_once_with(
            "What were the decisions?", transcript_id="t1")

        with pytest.raises(McpError, match="op='continue_thread' requires"):
            fn(op="continue_thread", thread_id="th1")
        cls.return_value.continue_askfred_thread.return_value = {"id": "msg2"}
        fn(op="continue_thread", thread_id="th1", query="And who owns it?")
        cls.return_value.continue_askfred_thread.assert_called_once_with("th1", "And who owns it?")

        with pytest.raises(McpError, match="op='delete_thread' requires"):
            fn(op="delete_thread")
        cls.return_value.delete_askfred_thread.return_value = {"id": "th1"}
        fn(op="delete_thread", thread_id="th1")
        cls.return_value.delete_askfred_thread.assert_called_once_with("th1")
    finally:
        patcher.stop()


# --- dispatch op= : fireflies_user / channel / bite / org --------------------------

def test_user_dispatches_by_op():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_user")).fn

        cls.return_value.get_user.return_value = {}
        fn(op="get")
        cls.return_value.get_user.assert_called_once_with(None)

        cls.return_value.list_users.return_value = []
        fn(op="list")
        cls.return_value.list_users.assert_called_once()

        cls.return_value.list_user_groups.return_value = []
        fn(op="groups", mine=True)
        cls.return_value.list_user_groups.assert_called_once_with(mine=True)

        with pytest.raises(McpError, match="op='set_role' requires"):
            fn(op="set_role")
        cls.return_value.set_user_role.return_value = {}
        fn(op="set_role", user_id="u1", role="admin")
        cls.return_value.set_user_role.assert_called_once_with("u1", "admin")
    finally:
        patcher.stop()


def test_channel_dispatches_by_op():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_channel")).fn
        with pytest.raises(McpError, match="op='get' requires"):
            fn(op="get")

        cls.return_value.get_channel.return_value = {}
        fn(op="get", channel_id="c1")
        cls.return_value.get_channel.assert_called_once_with("c1")

        cls.return_value.list_channels.return_value = []
        fn(op="list")
        cls.return_value.list_channels.assert_called_once()
    finally:
        patcher.stop()


def test_bite_dispatches_by_op():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_bite")).fn

        with pytest.raises(McpError, match="op='get' requires"):
            fn(op="get")
        cls.return_value.get_bite.return_value = {}
        fn(op="get", bite_id="b1")
        cls.return_value.get_bite.assert_called_once_with("b1")

        with pytest.raises(McpError, match="op='list' requires at least one"):
            fn(op="list")
        cls.return_value.list_bites.return_value = []
        fn(op="list", mine=True, limit=10)
        cls.return_value.list_bites.assert_called_once_with(
            mine=True, transcript_id=None, my_team=None, limit=10, skip=None)

        with pytest.raises(McpError, match="op='create' requires"):
            fn(op="create", transcript_id="t1")
        cls.return_value.create_bite.return_value = {}
        fn(op="create", transcript_id="t1", start_time=10.0, end_time=25.0, name="Clip")
        cls.return_value.create_bite.assert_called_once_with("t1", 10.0, 25.0, name="Clip")
    finally:
        patcher.stop()


def test_org_dispatches_by_op():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_org")).fn

        cls.return_value.list_contacts.return_value = []
        fn(op="contacts")
        cls.return_value.list_contacts.assert_called_once()

        cls.return_value.get_analytics.return_value = {}
        fn(op="analytics", start_time="2024-01-01", end_time="2024-01-31")
        cls.return_value.get_analytics.assert_called_once_with(
            start_time="2024-01-01", end_time="2024-01-31")

        cls.return_value.list_apps.return_value = {}
        fn(op="apps", app_id="app1", limit=5)
        cls.return_value.list_apps.assert_called_once_with(
            app_id="app1", transcript_id=None, skip=None, limit=5)

        cls.return_value.list_rule_executions_by_meeting.return_value = {}
        fn(op="rule_executions", limit=10)
        cls.return_value.list_rule_executions_by_meeting.assert_called_once_with(
            limit=10, cursor=None, logs_per_meeting=None, filters=None)

        with pytest.raises(McpError, match="op='audit_events' requires"):
            fn(op="audit_events")
        cls.return_value.list_audit_events.return_value = {}
        fn(op="audit_events", category="MEETING_OPERATIONS")
        cls.return_value.list_audit_events.assert_called_once_with(
            "MEETING_OPERATIONS", limit=None, cursor=None, action=None, date_from=None,
            date_to=None, actor_user_id=None, actor_email=None)
    finally:
        patcher.stop()


def test_upstream_error_types_are_mapped_without_crashing():
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.fireflies import FirefliesGraphQLError

    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("fireflies_transcript")).fn

        cls.return_value.get_transcript.side_effect = UpstreamHTTPError(401, {"error": "x"}, service="fireflies")
        with pytest.raises(McpError, match="rejected the API key"):
            fn(op="get", transcript_id="t1")

        cls.return_value.get_transcript.side_effect = FirefliesGraphQLError(
            [{"message": "not found", "extensions": {"code": "object_not_found"}}])
        with pytest.raises(McpError, match="not found"):
            fn(op="get", transcript_id="t1")
    finally:
        patcher.stop()
