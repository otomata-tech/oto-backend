"""Dispatch universel `oto_call` (ADR 0036) — appeler un outil NON listé par son nom.

Vérifie les invariants de l'ADR : (1) la rédaction est ré-appliquée à l'identique du
middleware (un outil à PII dispatché ne fuite pas), (2) les méta/spine sont refusés,
(3) un argument invalide renvoie le schéma (handoff §3), (4) l'erreur de la cible est
remontée en donnée, (5) le gate alpha est rejoué. On stub la résolution d'outil et la
policy de rédaction pour ne dépendre ni du serveur réel ni de la DB.
"""
import asyncio

import pytest
from fastmcp.tools.tool import ToolResult
from mcp.shared.exceptions import McpError
from mcp.types import TextContent
from oto.tools.common import FieldFilter

from oto_mcp import db, redaction
from oto_mcp.field_filter_defaults import _CANDIDATE_PII
from oto_mcp.server import mcp
from oto_mcp.tools import meta


# --- doubles ---------------------------------------------------------------

class _FakeTool:
    def __init__(self, name, *, result=None, exc=None, params=None):
        self.name = name
        self.description = f"fake {name}"
        self.parameters = params or {"type": "object", "properties": {}}
        self.output_schema = None
        self._result = result
        self._exc = exc

    async def run(self, arguments):
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeFastMCP:
    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self, run_middleware=False):
        return self._tools


class _FakeCtx:
    def __init__(self, tools):
        self.fastmcp = _FakeFastMCP(tools)


def _tool_result(payload: dict) -> ToolResult:
    import json
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
    )


_PROFILE = {
    "first_name": "Jean-Baptiste",
    "last_name": "Fleury",
    "email": "jb.fleury@example.com",
    "phone": "+33 6 12 34 56 78",
    "photo_url": "https://media.licdn.com/jb.jpg",
    "headline": "Head of Talent",
}


@pytest.fixture
def oto_call_fn():
    """La fonction `oto_call` telle qu'enregistrée (avec son paramètre `ctx`)."""
    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    return next(t.fn for t in tools if t.name == "oto_call")


def _call(fn, tools, **kw):
    return asyncio.run(fn(ctx=_FakeCtx(tools), **kw))


# --- 1. parité rédaction : PII dispatchée = PII redactée -------------------

def test_dispatch_redacts_pii_like_middleware(oto_call_fn, monkeypatch):
    # policy de rédaction candidat, injectée comme dans le test du middleware
    monkeypatch.setattr(redaction, "_resolve_field_filter",
                        lambda _s: FieldFilter(rules=_CANDIDATE_PII))
    target = _FakeTool("unipile_profile", result=_tool_result(dict(_PROFILE)))

    out = _call(oto_call_fn, [target], name="unipile_profile", arguments={})

    # sortie = ToolResult redacté sur les DEUX canaux, aucun brut résiduel
    import json
    sc = out.structured_content
    text = json.loads(out.content[0].text)
    for view in (sc, text):
        assert view["first_name"] != "Jean-Baptiste" and view["first_name"]
        assert view["last_name"] != "Fleury"
        assert "photo_url" not in view          # ré-identifiant direct supprimé
        assert view["headline"] == "Head of Talent"   # non sensible conservé
    assert "Fleury" not in out.content[0].text


def test_dispatch_passthrough_when_no_policy(oto_call_fn, monkeypatch):
    monkeypatch.setattr(redaction, "_resolve_field_filter", lambda _s: FieldFilter())
    payload = {"q": "spectacle", "count": 3}
    target = _FakeTool("fr_ccn_search", result=_tool_result(payload))

    out = _call(oto_call_fn, [target], name="fr_ccn_search", arguments={})
    # aucune policy → le ToolResult d'origine ressort INCHANGÉ (pas de re-sérialisation)
    assert out.structured_content == payload


# --- 2. refus des méta/spine ----------------------------------------------

@pytest.mark.parametrize("name", ["data_write", "run_start", "feedback", "oto_use_org"])
def test_dispatch_refuses_meta_spine(oto_call_fn, name):
    with pytest.raises(McpError) as ei:
        _call(oto_call_fn, [], name=name, arguments={})
    assert "méta/spine" in str(ei.value)


# --- 3. handoff de schéma sur argument invalide ---------------------------

def test_invalid_arguments_return_schema(oto_call_fn):
    from pydantic import BaseModel, ValidationError

    class _M(BaseModel):
        x: int

    try:
        _M(x="pas un int")
    except ValidationError as e:
        verr = e
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    target = _FakeTool("fr_loi_article", exc=verr, params=schema)

    with pytest.raises(McpError) as ei:
        _call(oto_call_fn, [target], name="fr_loi_article", arguments={"x": "bad"})
    data = ei.value.error.data
    assert data and data["input_schema"] == schema


# --- 4. l'erreur de la cible est remontée en donnée -----------------------

def test_target_error_returned_as_data(oto_call_fn, monkeypatch):
    monkeypatch.setattr(redaction, "_resolve_field_filter", lambda _s: FieldFilter())
    target = _FakeTool("foncier_dpe_adresse", exc=RuntimeError("upstream 500"))

    out = _call(oto_call_fn, [target], name="foncier_dpe_adresse", arguments={})
    assert out == {"tool": "foncier_dpe_adresse", "ok": False, "error": "upstream 500"}


def test_unknown_tool_raises(oto_call_fn, monkeypatch):
    monkeypatch.setattr(redaction, "_resolve_field_filter", lambda _s: FieldFilter())
    with pytest.raises(McpError) as ei:
        _call(oto_call_fn, [], name="fr_nexiste_pas", arguments={})
    assert "Unknown tool" in str(ei.value)


# --- 5. gate alpha rejoué sur la cible ------------------------------------

def test_alpha_gate_blocks_waitlisted(monkeypatch):
    from oto_mcp import session_visibility
    monkeypatch.setattr(session_visibility, "alpha_gate_enabled", lambda: True)
    monkeypatch.setattr(db, "get_user", lambda _sub: {"access_status": "waitlist"})
    with pytest.raises(McpError):
        meta._enforce_alpha_gate("sub-123", "fr_ccn_search")


def test_alpha_gate_allows_active(monkeypatch):
    from oto_mcp import session_visibility
    monkeypatch.setattr(session_visibility, "alpha_gate_enabled", lambda: True)
    monkeypatch.setattr(db, "get_user", lambda _sub: {"access_status": "active"})
    meta._enforce_alpha_gate("sub-123", "fr_ccn_search")  # ne lève pas


def test_alpha_gate_noop_without_flag(monkeypatch):
    from oto_mcp import session_visibility
    monkeypatch.setattr(session_visibility, "alpha_gate_enabled", lambda: False)
    # flag off → aucune lecture DB, aucun refus
    meta._enforce_alpha_gate("sub-123", "fr_ccn_search")
