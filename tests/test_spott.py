"""Connecteur Spott — ATS/CRM des cabinets de recrutement (api.gospott.com).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Recrutement), la
surface MCP curée sous le namespace `spott`, la jointure tool↔client oto-core
(garde version-skew), la sonde « tester la connexion », et les deux points où le
module fait autre chose que passer le plat : le routage de `spott_application`
(par job / par candidat / liste) et la bascule liste↔recherche de `spott_client`.

⚠️ La surface a été consolidée par `op=` (ADR 0047 §Amendement, 20 tools → 9) : les
noms attendus ci-dessous ont changé en conséquence. Le dispatch op par op (méthode
client appelée, refus d'une op inconnue, arguments obligatoires, garde-fous des 5
écritures) est verrouillé à part, dans `test_spott_op_dispatch.py`.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    # un tool par objet métier, le verbe en `op=`
    "spott_candidate", "spott_job", "spott_application", "spott_note",
    "spott_client",
    # laissés seuls : paramètres disjoints de ceux des objets ci-dessus
    "spott_stages", "spott_placements", "spott_people", "spott_users",
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
    """Enregistre le module seul et rend le tool demandé."""
    from fastmcp import FastMCP
    from oto_mcp.tools import spott

    m = FastMCP("t")
    spott.register(m)
    return asyncio.run(m.get_tool(name))


# --- registre -----------------------------------------------------------------

def test_spott_is_keyed_byo_only_connector():
    c = providers.REGISTRY["spott"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    # byo-only : pas de clé plateforme partagée pour un ATS client.
    assert "platform" not in c.auth_modes
    assert "spott" in providers.KEY_PROVIDERS
    assert c.category == "HR & recruiting"


def test_spott_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["spott"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_spott_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "spott"
               for t in all_tools if t.startswith("spott_"))


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("spott")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.spott.client import SpottClient
    for meth in ("list_candidates", "get_candidate", "search_candidates",
                 "create_candidate", "update_candidate",
                 "list_jobs", "get_job", "search_jobs",
                 "list_applications", "applications_by_candidate",
                 "applications_by_job", "create_application", "move_application",
                 "pipeline_stages", "list_notes", "create_note",
                 "list_clients", "get_client", "search_clients",
                 "list_client_contacts", "list_placements", "search_people",
                 "list_users"):
        assert callable(getattr(SpottClient, meth, None)), f"SpottClient.{meth} manquant"


# --- routage spott_application op="list" --------------------------------------

def _with_fake_client():
    """Patche la résolution de clé + la classe client ; rend le mock d'instance."""
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.spott.client.SpottClient")
    return key, cls


def test_applications_routes_by_job_candidate_or_listing():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("spott_application")

        tool.fn(op="list", job_id="v1")
        inst.applications_by_job.assert_called_once_with("v1")

        tool.fn(op="list", candidate_id="c1")
        inst.applications_by_candidate.assert_called_once_with("c1")

        tool.fn(op="list", limit=10)
        assert inst.list_applications.call_args.kwargs["limit"] == 10


def test_applications_rejects_job_and_candidate_together():
    from oto_mcp.mcp_errors import McpError
    key, cls = _with_fake_client()
    with key, cls:
        tool = _tool("spott_application")
        with pytest.raises(McpError):
            tool.fn(op="list", job_id="v1", candidate_id="c1")


# --- bascule liste ↔ recherche des clients ------------------------------------

def test_clients_switches_to_search_when_filters_given():
    """La bascule est passée d'implicite (présence de `filters`) à explicite
    (`op='search'`) — même capacité, verbe nommé."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("spott_client")

        tool.fn()
        inst.list_clients.assert_called_once()
        inst.search_clients.assert_not_called()

        flt = [{"type": "text", "operator": "contains",
                "path": "client.company.name", "value": "acme"}]
        tool.fn(op="search", filters=flt, page=1)
        inst.search_clients.assert_called_once_with(filters=flt, page=1, page_size=None)


# --- traduction des refus amont ----------------------------------------------

def test_upstream_401_becomes_a_readable_tool_error():
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.list_users.side_effect = UpstreamHTTPError(
            401, {"message": "invalid api key"}, service="spott")
        tool = _tool("spott_users")
        with pytest.raises(McpError, match="API key"):
            tool.fn()


def test_unknown_pipeline_is_a_param_error_not_a_crash():
    from oto_mcp.mcp_errors import McpError
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.pipeline_stages.side_effect = ValueError(
            "pipeline Spott inconnu : 'candidates'")
        tool = _tool("spott_stages")
        with pytest.raises(McpError, match="pipeline Spott inconnu"):
            tool.fn(entity="candidates")
