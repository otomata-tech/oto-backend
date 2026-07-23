"""Connecteur LightOn (API v3) — verrouille : l'entrée registre (credential
multi-champs clé + base_url + workspace_id par défaut, BYO user/org SEULEMENT
— le compte LightOn appartient au client, pas de mode plateforme), la surface
MCP curée (8 tools), la jointure tool↔client oto-core (garde version-skew),
le scoping workspace par défaut de l'instance (l'argument explicite prime),
et la traduction des erreurs LightOn en McpError actionnables.
"""
import asyncio
from unittest.mock import patch

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "lighton_search",
    "lighton_ask",
    "lighton_parse",
    "lighton_extract",
    "lighton_files",
    "lighton_upload_document",
    "lighton_delete_document",
    "lighton_workspaces",
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

def test_lighton_is_fields_connector():
    c = providers.REGISTRY["lighton"]
    assert c.kind == "tools"
    assert c.secret_kind == "fields"
    field_names = [f.name for f in c.secret_fields]
    assert field_names == ["api_key", "base_url", "workspace_id"]
    by_name = {f.name: f for f in c.secret_fields}
    assert by_name["api_key"].secret is True
    # base_url + workspace_id = config non-secrète OPTIONNELLE (instance
    # privée / workspace par défaut de l'instance)
    for opt in ("base_url", "workspace_id"):
        assert by_name[opt].secret is False
        assert by_name[opt].required is False


def test_lighton_is_byo_only_no_platform_mode():
    c = providers.REGISTRY["lighton"]
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})


def test_lighton_deny_by_default():
    c = providers.REGISTRY["lighton"]
    assert c.default_active is False


# --- surface MCP --------------------------------------------------------------

def test_lighton_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "lighton"
               for t in all_tools if t.startswith("lighton_"))


def test_lighton_v2_tools_are_gone(all_tools):
    # L'API v2 Paradigm (chat alfred, query, ask-question par doc) est
    # dépréciée — ses tools ne doivent plus exister.
    for legacy in ("lighton_chat", "lighton_models", "lighton_query",
                   "lighton_ask_document"):
        assert legacy not in all_tools


def test_lighton_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.lighton import LightOnClient
    for meth in ("search", "ask", "parse_bytes", "extract_bytes",
                 "list_files", "get_file", "upload_file_bytes", "delete_file",
                 "list_workspaces"):
        assert callable(getattr(LightOnClient, meth, None)), \
            f"LightOnClient.{meth} manquant"


# --- contrat via le tool layer (mocké) ----------------------------------------

_CREDS = {"api_key": "k", "base_url": "", "workspace_id": ""}


@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: dict(_CREDS),
    )


class _Resp:
    def __init__(self, payload, status=200):
        import json as _json
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.content = _json.dumps(payload).encode() if payload is not None else b""
        self.text = _json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


def _call(tool_name, **kwargs):
    from fastmcp import FastMCP
    from oto_mcp.tools import lighton as lighton_tool

    m = FastMCP("t")
    lighton_tool.register(m)
    fn = asyncio.run(m.get_tool(tool_name)).fn
    return fn(**kwargs)


def test_search_sends_bearer_and_body():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"results": []})
        _call("lighton_search", query="code secret", workspace_ids=[7046],
              max_results=3)
    args, kwargs = req.call_args
    assert args[0] == "POST"
    assert args[1] == "https://api.lighton.ai/api/v3/search"
    assert kwargs["headers"]["Authorization"] == "Bearer k"
    assert kwargs["json"] == {"query": "code secret", "workspace_id": [7046],
                              "max_results": 3}


def test_search_uses_instance_default_workspace(monkeypatch):
    # workspace_id configuré sur le credential → scope par défaut du search.
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: {**_CREDS, "workspace_id": "7046"},
    )
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"results": []})
        _call("lighton_search", query="x")
    _, kwargs = req.call_args
    assert kwargs["json"]["workspace_id"] == [7046]


def test_search_explicit_workspace_overrides_default(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: {**_CREDS, "workspace_id": "7046"},
    )
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"results": []})
        _call("lighton_search", query="x", workspace_ids=[99])
    _, kwargs = req.call_args
    assert kwargs["json"]["workspace_id"] == [99]


def test_ask_body():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"answer": "42", "results": []})
        _call("lighton_ask", query="le code ?", file_ids=[7],
              model="mistral-large-latest")
    args, kwargs = req.call_args
    assert args[1] == "https://api.lighton.ai/api/v3/ask"
    assert kwargs["json"] == {"query": "le code ?", "stream": False,
                              "file_id": [7], "max_results": 5,
                              "model": "mistral-large-latest"}


def test_upload_without_workspace_errors_actionably():
    # Pas de workspace_id explicite NI configuré sur l'instance → erreur
    # actionnable AVANT tout appel réseau LightOn.
    with patch("oto.tools.lighton.client.requests.request") as req:
        with pytest.raises(McpError) as exc:
            _call("lighton_upload_document",
                  source={"kind": "url", "url": "https://x/f.pdf"})
    assert "workspace_id" in str(exc.value)


def test_delete_document_returns_ack():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp(None, 204)
        out = _call("lighton_delete_document", file_id=7)
    args, _ = req.call_args
    assert args[0] == "DELETE"
    assert out == {"deleted": 7}


def test_401_maps_to_actionable_message():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"error": "unauthorized", "code": 401}, 401)
        with pytest.raises(McpError) as exc:
            _call("lighton_workspaces")
    assert "invalide" in str(exc.value)


def test_5xx_maps_to_retry_message():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"error": "oops"}, 503)
        with pytest.raises(McpError) as exc:
            _call("lighton_workspaces")
    assert "indisponible" in str(exc.value)


def test_parse_bad_source_never_hits_network():
    with patch("oto.tools.lighton.client.requests.request") as req:
        with pytest.raises(McpError):
            _call("lighton_parse", source={"kind": "nope"})
    req.assert_not_called()
