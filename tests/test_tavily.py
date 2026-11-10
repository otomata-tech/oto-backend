"""Connecteur Tavily — recherche web et lecture de pages pour agent (api.tavily.com).

Verrouille : l'entrée de registre (keyed, byo + clé plateforme OUVERTE sans quota,
catégorie Prospection), la surface MCP sous le namespace `tavily`, la jointure
tool↔client oto-core (garde version-skew), la sonde « tester la connexion », et les
deux points où le module ne fait pas que passer le plat : la borne de pages d'un
map/crawl synchrone, et la traduction des refus Tavily (432/433) en message
actionnable.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {"tavily_search", "tavily_extract", "tavily_map", "tavily_crawl"}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name for t in asyncio.run(m._list_tools())}


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import tavily

    m = FastMCP("t")
    tavily.register(m)
    return asyncio.run(m.get_tool(name))


# --- registre -----------------------------------------------------------------

def test_tavily_is_keyed_with_open_platform_key():
    c = providers.REGISTRY["tavily"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    # socle de recherche web : clé plateforme utilisable sans grant, quota par
    # défaut 100/mois (0 serait ILLIMITÉ — falsy dans access/quotas.py, revue #407).
    assert c.platform_key_open is True
    assert c.default_quota == 100
    assert "tavily" in providers.KEY_PROVIDERS
    assert c.category == "Web search & scraping"
    assert c.publisher_name == "Tavily"


def test_tavily_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["tavily"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_tavily_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "tavily"
               for t in all_tools if t.startswith("tavily_"))


def test_every_tool_has_a_description():
    for name in EXPECTED_TOOLS:
        assert _tool(name).description, f"{name} sans description (docstring f-string ?)"


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("tavily")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.tavily.client import TavilyClient
    for meth in ("search", "extract", "crawl", "map_site"):
        assert callable(getattr(TavilyClient, meth, None)), f"TavilyClient.{meth} manquant"


# --- bornes et erreurs --------------------------------------------------------

def _with_fake_client():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.tavily.client.TavilyClient")
    return key, cls


def test_crawl_caps_pages_and_timeout_under_rest_invoke_limit():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        _tool("tavily_crawl").fn(url="https://acme.com", limit=5000)
        kw = inst.crawl.call_args.kwargs
        assert kw["limit"] == 100
        assert kw["timeout_s"] < 45

        _tool("tavily_map").fn(url="https://acme.com")
        assert inst.map_site.call_args.kwargs["limit"] == 50


def test_extract_rejects_empty_or_oversized_batch():
    from oto_mcp.mcp_errors import McpError
    key, cls = _with_fake_client()
    with key, cls:
        with pytest.raises(McpError, match="at least one"):
            _tool("tavily_extract").fn(urls=[])
        with pytest.raises(McpError, match="20 URLs"):
            _tool("tavily_extract").fn(urls=[f"https://a.com/{i}" for i in range(21)])


def test_search_asks_for_a_basic_answer_by_default():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        _tool("tavily_search").fn(query="acme")
        assert client_cls.return_value.search.call_args.kwargs["include_answer"] == "basic"


@pytest.mark.parametrize("status,needle", [(432, "plan"), (433, "pay-as-you-go"),
                                           (401, "API key")])
def test_upstream_refusals_become_actionable_tool_errors(status, needle):
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.search.side_effect = UpstreamHTTPError(
            status, {"detail": "nope"}, service="tavily")
        with pytest.raises(McpError, match=needle):
            _tool("tavily_search").fn(query="acme")
