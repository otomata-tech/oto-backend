"""Lemlist lead-lifecycle tools (create/launch/variables) added on top of the
native read-only surface. Locks in: the tool↔client join (version-skew guard),
that `lemlist_create_lead` filters None fields and merges custom variables into
the lead payload before calling the client, that `lemlist_launch_lead` is
masked by default (it pushes a lead into a live send — a bad LLM call
shouldn't do that by accident) while the other new tools stay visible, and
platform-usage recording on all three.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp.tool_visibility import DEFAULT_HIDDEN_TOOLS, namespace_of

EXPECTED_NEW_TOOLS = {
    "lemlist_create_lead", "lemlist_launch_lead", "lemlist_add_lead_variables",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    tools = asyncio.run(m._list_tools())
    return {t.name for t in tools}


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import lemlist

    m = FastMCP("t")
    lemlist.register(m)
    return asyncio.run(m.get_tool(name))


def _with_fake_client():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.lemlist.LemlistClient")
    return key, cls


# --- surface MCP --------------------------------------------------------------

def test_new_lemlist_tools_registered_under_namespace(all_tools):
    assert EXPECTED_NEW_TOOLS <= all_tools
    assert all(namespace_of(t) == "lemlist" for t in EXPECTED_NEW_TOOLS)


# --- jointure tool <-> client oto-core (garde version-skew) -------------------

def test_client_exposes_methods_called_by_new_tools():
    from oto.tools.lemlist import LemlistClient
    for meth in ("create_lead", "launch_lead", "add_lead_variables"):
        assert callable(getattr(LemlistClient, meth, None)), f"LemlistClient.{meth} manquant"


# --- visibilité : seul launch_lead est masqué par défaut ----------------------

def test_only_launch_lead_is_hidden_by_default():
    assert "lemlist_launch_lead" in DEFAULT_HIDDEN_TOOLS
    assert "lemlist_create_lead" not in DEFAULT_HIDDEN_TOOLS
    assert "lemlist_add_lead_variables" not in DEFAULT_HIDDEN_TOOLS


# --- lemlist_create_lead : shaping du payload ---------------------------------

def test_create_lead_drops_none_fields_and_merges_custom_variables():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_create_lead")

        tool.fn(
            campaign_id="camp_1", email="a@acme.fr", first_name="A",
            custom_variables={"industry": "SaaS"},
        )

        args, kwargs = inst.create_lead.call_args
        assert args[0] == "camp_1"
        lead = args[1]
        assert lead == {"email": "a@acme.fr", "firstName": "A", "industry": "SaaS"}
        assert kwargs == {
            "deduplicate": False, "linkedin_enrichment": False,
            "find_email": False, "verify_email": False, "find_phone": False,
        }


def test_create_lead_forwards_enrichment_flags():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_create_lead")

        tool.fn(campaign_id="camp_1", email="a@acme.fr", find_email=True, deduplicate=True)

        kwargs = inst.create_lead.call_args.kwargs
        assert kwargs["find_email"] is True
        assert kwargs["deduplicate"] is True
        assert kwargs["find_phone"] is False


# --- lemlist_launch_lead / lemlist_add_lead_variables : passthrough -----------

def test_launch_lead_delegates_to_client():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.launch_lead.return_value = {"ok": True}
        tool = _tool("lemlist_launch_lead")

        out = tool.fn(lead_id="lea_1")

        inst.launch_lead.assert_called_once_with("lea_1")
        assert out == {"ok": True}


def test_add_lead_variables_delegates_to_client():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.add_lead_variables.return_value = {"ok": True}
        tool = _tool("lemlist_add_lead_variables")

        out = tool.fn(lead_id="lea_1", variables={"customField1": "x"})

        inst.add_lead_variables.assert_called_once_with("lea_1", {"customField1": "x"})
        assert out == {"ok": True}
