"""Connecteur LightOn Paradigm — verrouille : l'entrée registre (credential
multi-champs clé + base_url optionnelle, BYO user/org SEULEMENT — le compte
Paradigm appartient au client, pas de mode plateforme), la surface MCP curée
(7 tools), la jointure tool↔client oto-core (garde version-skew), le trim des
templates verbeux de `lighton_models`, et la traduction des erreurs Paradigm
en McpError actionnables (401 clé / 403 droits du compte / 5xx retry).
"""
import asyncio
from unittest.mock import patch

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "lighton_models",
    "lighton_chat",
    "lighton_query",
    "lighton_files",
    "lighton_ask_document",
    "lighton_upload_document",
    "lighton_delete_document",
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
    assert field_names == ["api_key", "base_url"]
    by_name = {f.name: f for f in c.secret_fields}
    assert by_name["api_key"].secret is True
    # base_url = config non-secrète OPTIONNELLE (instance privée seulement)
    assert by_name["base_url"].secret is False
    assert by_name["base_url"].required is False


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


def test_lighton_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.lighton import LightOnClient
    for meth in ("list_models", "chat", "query", "list_files", "get_file",
                 "upload_file_bytes", "ask_document", "delete_file"):
        assert callable(getattr(LightOnClient, meth, None)), \
            f"LightOnClient.{meth} manquant"


# --- contrat via le tool layer (mocké) ----------------------------------------

@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: {"api_key": "k", "base_url": ""},
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


def test_models_trims_verbose_templates():
    raw = {"object": "list", "data": [{
        "name": "alfred-ft5", "model_type": "Vision Language Model",
        "enabled": True,
        "start_messages_template": "x" * 5000,
        "prompt_template": "y" * 5000,
    }]}
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp(raw)
        out = _call("lighton_models")
    assert out["data"] == [{"name": "alfred-ft5",
                            "model_type": "Vision Language Model",
                            "enabled": True}]


def test_chat_sends_bearer_and_model():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"choices": [], "usage": {}})
        _call("lighton_chat",
              messages=[{"role": "user", "content": "hi"}],
              model="alfred-ft5", max_tokens=10)
    args, kwargs = req.call_args
    assert args[0] == "POST"
    assert args[1] == "https://paradigm.lighton.ai/api/v2/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer k"
    assert kwargs["json"]["model"] == "alfred-ft5"
    assert kwargs["json"]["max_tokens"] == 10


def test_query_body():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"chunks": []})
        _call("lighton_query", query="code secret", n=3)
    args, kwargs = req.call_args
    assert args[1] == "https://paradigm.lighton.ai/api/v2/query"
    assert kwargs["json"] == {"query": "code secret", "n": 3}


def test_ask_document_path():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"answer": "42"})
        _call("lighton_ask_document", file_id=7, question="le code ?")
    args, kwargs = req.call_args
    assert args[1] == "https://paradigm.lighton.ai/api/v2/files/7/ask-question"
    assert kwargs["json"] == {"question": "le code ?"}


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
            _call("lighton_models")
    assert "invalide" in str(exc.value)


def test_403_maps_to_paradigm_rights_message():
    # Vécu 2026-07-23 : l'upload renvoie 403 selon le rôle/plan du compte
    # Paradigm — le message doit pointer LightOn, pas le connecteur.
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"error": "Permission denied", "code": 403}, 403)
        with pytest.raises(McpError) as exc:
            _call("lighton_files")
    assert "Paradigm" in str(exc.value)


def test_5xx_maps_to_retry_message():
    with patch("oto.tools.lighton.client.requests.request") as req:
        req.return_value = _Resp({"error": "oops"}, 503)
        with pytest.raises(McpError) as exc:
            _call("lighton_models")
    assert "indisponible" in str(exc.value)


def test_upload_bad_source_never_hits_network():
    with patch("oto.tools.lighton.client.requests.request") as req:
        with pytest.raises(McpError):
            _call("lighton_upload_document", source={"kind": "nope"})
    req.assert_not_called()
