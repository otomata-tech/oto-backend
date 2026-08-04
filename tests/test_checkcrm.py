"""Check CRM connector — wraps Julien's "enrichment" job-change-check API
(https://enrichment-two.vercel.app/v1). Locks in: the registry entry (keyed
byo-only api_key, no platform mode), the curated MCP surface under the
`checkcrm` namespace (not `check_crm` — see providers.py comment on
namespace_of), the tool↔client join (version-skew guard), the client's request
shaping (X-API-Key header, not Authorization), and the add_subsidiary
solo/batch merge (batch aborts whole-hog on a 401/403, per-item error
otherwise).
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "checkcrm_send_contacts",
    "checkcrm_add_subsidiary",
    "checkcrm_list_subsidiaries",
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

def test_checkcrm_is_keyed_byo_only_connector():
    c = providers.REGISTRY["checkcrm"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "checkcrm" in providers.KEY_PROVIDERS


# --- surface MCP --------------------------------------------------------------

def test_checkcrm_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "checkcrm"
               for t in all_tools if t.startswith("checkcrm_"))


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.checkcrm.client import CheckCrmClient
    for meth in ("send_contacts", "add_subsidiary", "list_subsidiaries"):
        assert callable(getattr(CheckCrmClient, meth, None)), f"CheckCrmClient.{meth} manquant"


# --- contrat du client (HTTP mocké) -------------------------------------------

def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.content = b"{}" if body is not None else (b"" if status >= 400 else b"{}")
    r.json.return_value = body if body is not None else {}
    r.text = "" if body is None else str(body)
    return r


def test_client_sends_x_api_key_header_not_bearer():
    from oto.tools.checkcrm.client import CheckCrmClient
    with patch("oto.tools.checkcrm.client.requests.request") as req:
        req.return_value = _resp(200, {"checkId": "abc", "contactCount": 1, "skippedCount": 0})
        out = CheckCrmClient(api_key="secret").send_contacts(
            "acc-1", [{"id": "c1", "linkedinUrl": "https://www.linkedin.com/in/jane"}]
        )
    assert out == {"checkId": "abc", "contactCount": 1, "skippedCount": 0}
    args, kwargs = req.call_args
    assert kwargs["headers"]["X-API-Key"] == "secret"
    assert "Authorization" not in kwargs["headers"]
    assert args[0] == "POST"
    assert args[1] == "https://enrichment-two.vercel.app/v1/contacts"
    assert kwargs["json"] == {
        "accountId": "acc-1",
        "contacts": [{"id": "c1", "linkedinUrl": "https://www.linkedin.com/in/jane"}],
    }


def test_send_contacts_includes_account_linkedin_url_when_given():
    from oto.tools.checkcrm.client import CheckCrmClient
    with patch("oto.tools.checkcrm.client.requests.request") as req:
        req.return_value = _resp(200, {"checkId": "x", "contactCount": 0, "skippedCount": 0})
        CheckCrmClient(api_key="k").send_contacts(
            "acc-1", [], account_linkedin_url="https://www.linkedin.com/company/acme-corp"
        )
    _, kwargs = req.call_args
    assert kwargs["json"]["accountLinkedinUrl"] == "https://www.linkedin.com/company/acme-corp"


def test_list_subsidiaries_sends_get_with_slug_filter():
    """L'endpoint filtre par `slug`/`name` (oto-core#31) — plus par le
    `companyLinkedinUrl` du parent."""
    from oto.tools.checkcrm.client import CheckCrmClient
    with patch("oto.tools.checkcrm.client.requests.request") as req:
        req.return_value = _resp(200, {"companies": []})
        CheckCrmClient(api_key="k").list_subsidiaries(slug="acme-corp")
    args, kwargs = req.call_args
    assert args[0] == "GET"
    assert args[1] == "https://enrichment-two.vercel.app/v1/companies/subsidiaries"
    assert kwargs["params"] == {"slug": "acme-corp"}


def test_list_subsidiaries_without_filter_sends_no_param():
    """Sans filtre = tous les parents du réseau, pas une erreur d'argument manquant."""
    from oto.tools.checkcrm.client import CheckCrmClient
    with patch("oto.tools.checkcrm.client.requests.request") as req:
        req.return_value = _resp(200, {"companies": []})
        CheckCrmClient(api_key="k").list_subsidiaries()
    _, kwargs = req.call_args
    assert kwargs["params"] == {}


def test_client_4xx_raises_upstream_error_with_body_preserved():
    from oto.tools.checkcrm.client import CheckCrmClient
    from oto.tools.common.errors import UpstreamHTTPError

    with patch("oto.tools.checkcrm.client.requests.request") as req:
        req.return_value = _resp(400, {"error": "companyLinkedinUrl is a numeric LinkedIn company ID"})
        with pytest.raises(UpstreamHTTPError) as exc_info:
            CheckCrmClient(api_key="k").add_subsidiary("https://www.linkedin.com/company/12345",
                                                        "https://www.linkedin.com/company/acme-labs")
    assert exc_info.value.status_code == 400
    assert "numeric LinkedIn company ID" in str(exc_info.value.body)


# --- checkcrm_add_subsidiary : merge solo/bulk --------------------------------

def test_add_subsidiary_rejects_both_or_neither_of_solo_and_batch():
    from mcp.shared.exceptions import McpError
    from oto_mcp.tools import checkcrm
    from fastmcp import FastMCP

    m = FastMCP("t")
    checkcrm.register(m)
    tool = asyncio.run(m.get_tool("checkcrm_add_subsidiary"))

    with pytest.raises(McpError):
        tool.fn(company_linkedin_url="https://www.linkedin.com/company/acme-corp")
    with pytest.raises(McpError):
        tool.fn(
            company_linkedin_url="https://www.linkedin.com/company/acme-corp",
            subsidiary_linkedin_url="https://www.linkedin.com/company/acme-labs",
            subsidiary_linkedin_urls=["https://www.linkedin.com/company/other"],
        )


def test_add_subsidiary_batch_reports_per_item_failures_but_continues():
    from oto_mcp.tools import checkcrm
    from oto.tools.common.errors import UpstreamHTTPError
    from fastmcp import FastMCP

    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)):
        with patch("oto.tools.checkcrm.client.CheckCrmClient") as client_cls:
            inst = client_cls.return_value
            inst.add_subsidiary.side_effect = [
                {"subsidiary": {"id": "1"}, "duplicate": False},
                UpstreamHTTPError(422, "bad slug"),
                {"subsidiary": {"id": "3"}, "duplicate": True},
            ]
            m = FastMCP("t")
            checkcrm.register(m)
            tool = asyncio.run(m.get_tool("checkcrm_add_subsidiary"))
            result = tool.fn(
                company_linkedin_url="https://www.linkedin.com/company/acme-corp",
                subsidiary_linkedin_urls=[
                    "https://www.linkedin.com/company/a",
                    "https://www.linkedin.com/company/b",
                    "https://www.linkedin.com/company/c",
                ],
            )

    assert result["total"] == 3
    assert result["succeeded"] == 2
    assert len(result["failed"]) == 1
    assert result["failed"][0]["index"] == 1
    assert "bad slug" in result["failed"][0]["error"]


def test_add_subsidiary_batch_aborts_whole_batch_on_401():
    from oto_mcp.tools import checkcrm
    from oto.tools.common.errors import UpstreamHTTPError
    from fastmcp import FastMCP

    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)):
        with patch("oto.tools.checkcrm.client.CheckCrmClient") as client_cls:
            inst = client_cls.return_value
            inst.add_subsidiary.side_effect = [
                {"subsidiary": {"id": "1"}, "duplicate": False},
                UpstreamHTTPError(401, "bad api key"),
                {"subsidiary": {"id": "3"}, "duplicate": False},
            ]
            m = FastMCP("t")
            checkcrm.register(m)
            tool = asyncio.run(m.get_tool("checkcrm_add_subsidiary"))
            with pytest.raises(UpstreamHTTPError):
                tool.fn(
                    company_linkedin_url="https://www.linkedin.com/company/acme-corp",
                    subsidiary_linkedin_urls=[
                        "https://www.linkedin.com/company/a",
                        "https://www.linkedin.com/company/b",
                        "https://www.linkedin.com/company/c",
                    ],
                )
    # only the first two side_effect entries should have been consumed —
    # the batch aborted before reaching the third.
    assert inst.add_subsidiary.call_count == 2
