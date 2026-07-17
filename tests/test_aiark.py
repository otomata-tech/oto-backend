"""Connecteur AI Ark — requalifié de mount fédéré (#152) en connecteur classique
`kind="tools"` (#160). Verrouille : l'entrée registre (keyed API, mode plateforme,
plus de mount), la surface MCP curée, la jointure tool↔client oto-core (garde
version-skew pour un module `_client()->tuple`, hors périmètre de la sonde AST) et
le contrat du client (shaping des requêtes + 404 = introuvable, pas une erreur).
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "aiark_credits",
    "aiark_company_search",
    "aiark_people_search",
    "aiark_export_person",
    "aiark_reverse_lookup",
    "aiark_mobile_phone",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    tools = asyncio.run(m._list_tools())
    return {t.name for t in tools}


# --- registre -----------------------------------------------------------------

def test_aiark_is_classic_keyed_connector():
    c = providers.REGISTRY["aiark"]
    assert c.kind == "tools"            # plus un mount fédéré
    assert c.mount_url is None          # entrée mount retirée
    assert c.keyed and c.secret_kind == "api_key"
    assert "aiark" in providers.KEY_PROVIDERS


def test_aiark_supports_platform_mode():
    c = providers.REGISTRY["aiark"]
    # mode plateforme désormais possible (record_platform_usage dans les handlers)
    assert "platform" in c.auth_modes
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})


def test_aiark_no_longer_a_mount():
    assert all(c.name != "aiark" for c in providers.MOUNT_CONNECTORS)


# --- surface MCP --------------------------------------------------------------

def test_aiark_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "aiark"
               for t in all_tools if t.startswith("aiark_"))


def test_aiark_verify_is_probe_not_tool(all_tools):
    # « tester la connexion » = sonde générique (oto_instance op=verify), plus un
    # tool MCP dédié par connecteur.
    from oto_mcp import connector_verify
    assert "aiark_verify_key" not in all_tools
    assert connector_verify.supports("aiark")


def test_aiark_async_bulk_endpoints_not_exposed(all_tools):
    # v1 = synchrone seulement ; les exports/find-emails EN LOT (webhook) sont hors
    # périmètre → aucun tool "bulk"/"track" exposé.
    assert not any("bulk" in t or "track" in t for t in all_tools
                   if t.startswith("aiark_"))


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.aiark.client import AiArkClient
    for meth in ("verify_key", "credits", "search_companies", "search_people",
                 "export_person", "reverse_lookup", "mobile_phone"):
        assert callable(getattr(AiArkClient, meth, None)), f"AiArkClient.{meth} manquant"


# --- contrat du client (HTTP mocké) -------------------------------------------

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


def test_client_sends_x_token_and_json():
    from oto.tools.aiark.client import AiArkClient
    with patch("oto.tools.aiark.client.requests.request") as req:
        req.return_value = _resp(200, {"total": 42})
        out = AiArkClient(api_key="secret").credits()
    assert out == {"total": 42}
    _, kwargs = req.call_args
    assert kwargs["headers"]["X-TOKEN"] == "secret"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert req.call_args[0] == ("GET",
                                "https://api.ai-ark.com/api/developer-portal/v1/payments/credits")


def test_search_people_body_shape():
    from oto.tools.aiark.client import AiArkClient
    with patch("oto.tools.aiark.client.requests.request") as req:
        req.return_value = _resp(200, {"content": [], "totalElements": 0})
        AiArkClient(api_key="k").search_people(
            contact={"seniority": {"any": {"include": ["founder"]}}},
            page=2, size=50)
    _, kwargs = req.call_args
    assert kwargs["json"] == {
        "page": 2, "size": 50,
        "contact": {"seniority": {"any": {"include": ["founder"]}}},
    }


def test_export_person_404_returns_none():
    from oto.tools.aiark.client import AiArkClient
    with patch("oto.tools.aiark.client.requests.request") as req:
        req.return_value = _resp(404)
        assert AiArkClient(api_key="k").export_person(url="https://lnkd.in/x") is None


def test_export_person_requires_id_or_url():
    from oto.tools.aiark.client import AiArkClient
    with pytest.raises(ValueError):
        AiArkClient(api_key="k").export_person()


def test_mobile_phone_requires_linkedin_or_domain_name():
    from oto.tools.aiark.client import AiArkClient
    with pytest.raises(ValueError):
        AiArkClient(api_key="k").mobile_phone(domain="acme.com")  # name manquant
