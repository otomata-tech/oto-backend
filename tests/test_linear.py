"""Connecteur Linear — issues, comments, projects, teams, cycles, labels,
users, webhooks (api.linear.app/graphql).

Verrouille : l'entrée de registre (keyed byo_org-only, catégorie Métier), la
doc how-to, la surface MCP (8 tools, chacun avec une description — régression
du piège f-string-docstring), la sonde « tester la connexion », la jointure
tool↔client oto-core (garde version-skew), et le dispatch `op=` (required
manquant refusé, arg non pertinent pour CET op refusé — y compris `first`/
`after` sur un op qui ne pagine pas, cf. le bug corrigé en session).
"""
import asyncio

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import linear

EXPECTED_TOOLS = {
    "linear_issue", "linear_comment", "linear_project", "linear_team",
    "linear_cycle", "linear_label", "linear_user", "linear_webhook",
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
    """Enregistre le module avec `LinearClient` mocké, DANS le patch (sinon
    `register()`'s `from ... import LinearClient` capture la vraie classe
    avant que le patch ne s'applique)."""
    from unittest.mock import patch
    from fastmcp import FastMCP

    patcher = patch("oto.tools.linear.client.LinearClient")
    cls = patcher.start()
    m = FastMCP("t")
    linear.register(m)
    return m, cls, patcher


# --- registre -----------------------------------------------------------------

def test_linear_is_keyed_byo_user_and_byo_org_connector():
    """Une clé API Linear est personnelle (elle agit au nom de son porteur) :
    elle se pose pour soi ou pour l'org, jamais comme clé plateforme (24/09)."""
    c = providers.REGISTRY["linear"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert "linear" in providers.KEY_PROVIDERS
    assert c.category == "Product & data"
    assert c.publisher_name == "Linear"
    assert c.label == "Linear"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["linear"] == "linear.app"


def test_linear_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["linear"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------------

def test_linear_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "linear" for t in all_tools if t.startswith("linear_"))


def test_linear_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    # The patcher is stopped: left running, it handed a mocked `LinearClient`
    # to every later test of the same worker.
    _, _, patcher = _fn_with_mock_client()
    patcher.stop()
    assert connector_verify.supports("linear")


# --- jointure tool ↔ client oto-core (garde version-skew) -------------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.linear.client import LinearClient
    for meth in (
        "get_issue", "list_issues", "search_issues", "create_issue", "update_issue",
        "archive_issue", "delete_issue",
        "get_comment", "list_comments", "create_comment", "update_comment", "delete_comment",
        "get_project", "list_projects", "create_project", "update_project",
        "get_team", "list_teams", "list_workflow_states",
        "get_cycle", "list_cycles",
        "get_label", "list_labels", "create_label",
        "get_viewer", "get_user", "list_users",
        "list_webhooks", "create_webhook", "update_webhook", "delete_webhook",
    ):
        assert callable(getattr(LinearClient, meth, None)), f"LinearClient.{meth} manquant"


# --- dispatch op= (comportement) : linear_issue ------------------------------------

def test_issue_list_refuses_non_list_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_issue")).fn
        with pytest.raises(McpError, match="op='list' does not use"):
            fn(op="list", title="oops")

        cls.return_value.list_issues.return_value = {"nodes": [], "pageInfo": {}}
        fn(op="list", team_id="t1")
        # Les bornes de date et `order_by` (signaux #561/#568) voyagent aussi,
        # à None quand l'appelant n'en veut pas — cf. test_linear_issue_window.py.
        cls.return_value.list_issues.assert_called_once_with(
            team_id="t1", project_id=None, cycle_id=None, assignee_id=None,
            state_id=None, updated_after=None, updated_before=None,
            created_after=None, created_before=None, order_by=None,
            first=50, after=None)
    finally:
        patcher.stop()


def test_issue_list_refuses_first_on_a_non_paginating_op():
    """Regression: an earlier draft hardcoded `first=None` inside the `_only`
    guard for create/update, which meant a caller-supplied `first` on those
    ops was never actually checked (the real value was discarded before the
    guard saw it). `get` locks the general case: any op outside list/search
    must refuse a stray `first`/`after`, not silently swallow it."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_issue")).fn
        with pytest.raises(McpError, match="op='get' does not use"):
            fn(op="get", issue_id="i1", first=10)
        cls.return_value.get_issue.assert_not_called()
    finally:
        patcher.stop()


def test_issue_get_requires_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_issue")).fn
        with pytest.raises(McpError, match="op='get' requires"):
            fn(op="get")

        cls.return_value.get_issue.return_value = {"id": "i1"}
        result = fn(op="get", issue_id="i1")
        assert result == {"id": "i1"}
        cls.return_value.get_issue.assert_called_once_with("i1")
    finally:
        patcher.stop()


def test_issue_search_requires_query_and_paginates_with_default(monkeypatch):
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_issue")).fn
        with pytest.raises(McpError, match="op='search' requires"):
            fn(op="search")

        cls.return_value.search_issues.return_value = {"nodes": [], "pageInfo": {}}
        fn(op="search", query="bug")
        cls.return_value.search_issues.assert_called_once_with(
            "bug", team_id=None, first=50, after=None)
    finally:
        patcher.stop()


def test_issue_create_requires_title_and_team_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_issue")).fn
        with pytest.raises(McpError, match="op='create' requires"):
            fn(op="create", title="Bug")

        cls.return_value.create_issue.return_value = {"success": True}
        fn(op="create", title="Bug", team_id="t1", priority=2)
        cls.return_value.create_issue.assert_called_once_with(
            "Bug", "t1", description=None, assignee_id=None, state_id=None,
            priority=2, label_ids=None, project_id=None, cycle_id=None,
            parent_id=None, due_date=None, estimate=None)
    finally:
        patcher.stop()


def test_issue_update_requires_id_and_refuses_create_only_params():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_issue")).fn
        with pytest.raises(McpError, match="op='update' requires"):
            fn(op="update")
        with pytest.raises(McpError, match="op='update' does not use"):
            fn(op="update", issue_id="i1", parent_id="i0")

        cls.return_value.update_issue.return_value = {"success": True}
        fn(op="update", issue_id="i1", state_id="s2")
        cls.return_value.update_issue.assert_called_once_with(
            "i1", title=None, description=None, assignee_id=None, state_id="s2",
            priority=None, label_ids=None, project_id=None, cycle_id=None,
            due_date=None, estimate=None)
    finally:
        patcher.stop()


def test_issue_archive_and_delete_require_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_issue")).fn
        with pytest.raises(McpError, match="op='archive' requires"):
            fn(op="archive")
        with pytest.raises(McpError, match="op='delete' requires"):
            fn(op="delete")

        cls.return_value.archive_issue.return_value = {"success": True}
        fn(op="archive", issue_id="i1")
        cls.return_value.archive_issue.assert_called_once_with("i1")

        cls.return_value.delete_issue.return_value = {"success": True}
        fn(op="delete", issue_id="i1")
        cls.return_value.delete_issue.assert_called_once_with("i1")
    finally:
        patcher.stop()


# --- dispatch op= : linear_comment / linear_team / linear_webhook -----------------

def test_comment_create_requires_issue_id_and_body():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_comment")).fn
        with pytest.raises(McpError, match="op='create' requires"):
            fn(op="create", issue_id="i1")

        cls.return_value.create_comment.return_value = {"success": True}
        fn(op="create", issue_id="i1", body="lgtm")
        cls.return_value.create_comment.assert_called_once_with("i1", "lgtm", parent_id=None)
    finally:
        patcher.stop()


def test_team_states_requires_team_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_team")).fn
        with pytest.raises(McpError, match="op='states' requires"):
            fn(op="states")

        cls.return_value.list_workflow_states.return_value = {"nodes": [], "pageInfo": {}}
        fn(op="states", team_id="t1")
        cls.return_value.list_workflow_states.assert_called_once_with("t1", first=50, after=None)
    finally:
        patcher.stop()


def test_webhook_create_requires_url_and_defaults_enabled_true():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_webhook")).fn
        with pytest.raises(McpError, match="op='create' requires"):
            fn(op="create")

        cls.return_value.create_webhook.return_value = {"success": True}
        fn(op="create", url="https://example.com/hook", team_id="t1",
           resource_types=["Issue"])
        cls.return_value.create_webhook.assert_called_once_with(
            "https://example.com/hook", team_id="t1", resource_types=["Issue"],
            secret=None, enabled=True, all_public_teams=False)
    finally:
        patcher.stop()


def test_webhook_update_and_delete_require_webhook_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("linear_webhook")).fn
        with pytest.raises(McpError, match="op='update' requires"):
            fn(op="update")
        with pytest.raises(McpError, match="op='delete' requires"):
            fn(op="delete")

        cls.return_value.delete_webhook.return_value = {"success": True}
        fn(op="delete", webhook_id="w1")
        cls.return_value.delete_webhook.assert_called_once_with("w1")
    finally:
        patcher.stop()
