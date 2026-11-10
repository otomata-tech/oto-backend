"""Connecteur Firecrawl — pages web en markdown propre (api.firecrawl.dev).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection), la
surface MCP sous le namespace `firecrawl`, la jointure tool↔client oto-core (garde
version-skew), la sonde « tester la connexion », et le point où le module ne fait
pas que passer le plat : `firecrawl_crawl_status` accepte soit un id, soit l'URL
`next` de la tranche suivante — les deux mappent sur le même argument client.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "firecrawl_scrape", "firecrawl_map", "firecrawl_search",
    "firecrawl_crawl", "firecrawl_crawl_status", "firecrawl_cancel_crawl",
    "firecrawl_extract", "firecrawl_extract_status",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name for t in asyncio.run(m._list_tools())}


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import firecrawl

    m = FastMCP("t")
    firecrawl.register(m)
    return asyncio.run(m.get_tool(name))


# --- registre -----------------------------------------------------------------

def test_firecrawl_is_keyed_connector_platform_grant_only():
    c = providers.REGISTRY["firecrawl"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    # platform sur grant explicite (revente au crédit) : jamais en libre-service.
    assert c.platform_key_open is False
    assert c.default_quota == 0
    assert "firecrawl" in providers.KEY_PROVIDERS
    assert c.category == "Web search & scraping"
    assert c.publisher_name == "Firecrawl"


def test_firecrawl_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["firecrawl"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_firecrawl_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "firecrawl"
               for t in all_tools if t.startswith("firecrawl_"))


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("firecrawl")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.firecrawl.client import FirecrawlClient
    for meth in ("scrape", "crawl", "crawl_status", "cancel_crawl",
                 "map_site", "search", "extract", "extract_status"):
        assert callable(getattr(FirecrawlClient, meth, None)), \
            f"FirecrawlClient.{meth} manquant"


# --- pagination d'un crawl ----------------------------------------------------

def _with_fake_client():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.firecrawl.client.FirecrawlClient")
    return key, cls


def test_crawl_status_accepts_job_id_or_next_url():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("firecrawl_crawl_status")

        tool.fn(job_id="job-1")
        assert inst.crawl_status.call_args.kwargs == {
            "crawl_id": "job-1", "next_url": None}

        tool.fn(next_url="https://api.firecrawl.dev/v2/crawl/job-1?skip=10")
        assert inst.crawl_status.call_args.kwargs["next_url"].endswith("skip=10")


def test_upstream_402_becomes_an_actionable_tool_error():
    """Crédits épuisés : l'agent doit lire un message qui dit quoi faire, pas un 402 nu."""
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.scrape.side_effect = UpstreamHTTPError(
            402, {"error": "Payment Required"}, service="firecrawl")
        with pytest.raises(McpError, match="credits"):
            _tool("firecrawl_scrape").fn(url="https://acme.com")
