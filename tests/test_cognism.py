"""Connecteur Cognism — verrouille : l'entrée registre (keyed API, BYO user/org
SEULEMENT — pas de mode plateforme tant qu'il n'y a pas d'accord commercial
Otomata↔Cognism, contrairement à AI Ark/Kaspr), la surface MCP curée (8 tools),
la jointure tool↔client oto-core (garde version-skew), le contrat HTTP côté
tool layer (traduction erreurs -> McpError, y compris ValueError de filtre
invalide -> message actionnable AVANT tout appel réseau côté client), et les
contraintes XOR (redeem: ids/redeem_ids ; enrich: au moins un champ d'identité).
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "cognism_search_contacts",
    "cognism_search_accounts",
    "cognism_redeem_contacts",
    "cognism_redeem_accounts",
    "cognism_enrich_contact",
    "cognism_enrich_account",
    "cognism_contact_entitlement",
    "cognism_account_entitlement",
    "cognism_filter_values",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    tools = asyncio.run(m._list_tools())
    return {t.name: t for t in tools}


# --- registre -----------------------------------------------------------------

def test_cognism_is_classic_keyed_connector():
    c = providers.REGISTRY["cognism"]
    assert c.kind == "tools"
    assert c.mount_url is None
    assert c.keyed and c.secret_kind == "api_key"
    assert "cognism" in providers.KEY_PROVIDERS


def test_cognism_is_byo_only_no_platform_mode():
    # Pas d'accord commercial Otomata<->Cognism a ce jour : BYO user/org
    # seulement, contrairement a AI Ark/Kaspr qui ouvrent "platform".
    c = providers.REGISTRY["cognism"]
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes


def test_cognism_deny_by_default():
    c = providers.REGISTRY["cognism"]
    assert c.default_active is False


def test_cognism_no_longer_a_mount():
    assert all(c.name != "cognism" for c in providers.MOUNT_CONNECTORS)


# --- surface MCP --------------------------------------------------------------

def test_cognism_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "cognism"
               for t in all_tools if t.startswith("cognism_"))


def test_cognism_tools_all_have_descriptions(all_tools):
    # Régression du piège f-string-docstring (f"""..." n'alimente PAS
    # __doc__ -> FastMCP l'expose sans description).
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.cognism.client import CognismClient
    for meth in ("search_contacts", "search_accounts", "redeem_contacts",
                 "redeem_accounts", "enrich_contact", "enrich_account",
                 "contact_entitlement", "account_entitlement", "filter_values"):
        assert callable(getattr(CognismClient, meth, None)), \
            f"CognismClient.{meth} manquant"


# --- contrat HTTP (mocké) — via le tool layer ----------------------------------

def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.content = b"{}" if body is not None else b""
    r.json.return_value = body if body is not None else {}
    if status >= 400:
        from requests import HTTPError
        r.raise_for_status.side_effect = HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


def _register_and_get(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import cognism as cognism_tool

    m = FastMCP("t")
    cognism_tool.register(m)
    tools = asyncio.run(m._list_tools())
    return next(t for t in tools if t.name == name)


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False)
    )
    monkeypatch.setattr("oto_mcp.access.record_platform_usage", lambda provider: None)


def _call(tool_name, **kwargs):
    from fastmcp import FastMCP
    from oto_mcp.tools import cognism as cognism_tool

    m = FastMCP("t")
    cognism_tool.register(m)
    fn = asyncio.run(m.get_tool(tool_name)).fn
    return fn(**kwargs)


def test_search_contacts_sends_bearer_and_query_params():
    with patch("oto.tools.cognism.client.requests.request") as req:
        req.return_value = _resp(200, {"results": [], "totalResults": 0})
        _call("cognism_search_contacts", filters={"firstName": "Stjepan"},
              index_size=25, last_returned_key=None)
    args, kwargs = req.call_args
    assert args[0] == "POST"
    assert args[1] == "https://app.cognism.com/api/search/contact/search"
    assert kwargs["headers"]["Authorization"] == "Bearer k"
    assert kwargs["params"] == {"indexSize": 25, "lastReturnedKey": ""}
    assert kwargs["json"] == {"firstName": "Stjepan"}


def test_search_contacts_invalid_enum_never_hits_network():
    with patch("oto.tools.cognism.client.requests.request") as req:
        with pytest.raises(McpError) as exc:
            _call("cognism_search_contacts", filters={"seniority": ["Founder"]})
    req.assert_not_called()
    assert "seniority" in str(exc.value)


def test_redeem_contacts_requires_ids_or_redeem_ids():
    with pytest.raises(McpError):
        _call("cognism_redeem_contacts")


def test_redeem_contacts_body_shape():
    with patch("oto.tools.cognism.client.requests.request") as req:
        req.return_value = _resp(200, {"total": 0, "result": []})
        _call("cognism_redeem_contacts", ids=["abc"])
    args, kwargs = req.call_args
    assert args[1] == "https://app.cognism.com/api/search/contact/redeem"
    assert kwargs["json"] == {"ids": ["abc"]}
    assert kwargs["params"] == {"mergePhonesAndLocations": "false"}


def test_enrich_contact_requires_at_least_one_identity_field():
    with patch("oto.tools.cognism.client.requests.request") as req:
        with pytest.raises(McpError):
            _call("cognism_enrich_contact")
    req.assert_not_called()


def test_enrich_contact_body_shape():
    with patch("oto.tools.cognism.client.requests.request") as req:
        req.return_value = _resp(200, {"matchScore": 60, "results": []})
        _call("cognism_enrich_contact", email="stjepan.buljat@cognism.com")
    args, kwargs = req.call_args
    assert args[1] == "https://app.cognism.com/api/search/contact/enrich"
    assert kwargs["json"] == {"email": "stjepan.buljat@cognism.com"}


def test_401_maps_to_actionable_message():
    with patch("oto.tools.cognism.client.requests.request") as req:
        req.return_value = _resp(401)
        with pytest.raises(McpError) as exc:
            _call("cognism_contact_entitlement")
    assert "401" in str(exc.value) or "invalide" in str(exc.value)


def test_5xx_maps_to_retry_message():
    with patch("oto.tools.cognism.client.requests.request") as req:
        req.return_value = _resp(503)
        with pytest.raises(McpError) as exc:
            _call("cognism_account_entitlement")
    assert "503" in str(exc.value)


def test_filter_values_unknown_kind_maps_to_mcp_error():
    with patch("oto.tools.cognism.client.requests.request") as req:
        with pytest.raises(McpError):
            _call("cognism_filter_values", kind="bogus")
    req.assert_not_called()
