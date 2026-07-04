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


# --- 6. jeton `org=` (ADR 0038) : posé pendant run, nettoyé après ----------

class _OrgCapturingTool(_FakeTool):
    """Capture l'org épinglée (`_CALL_ORG`) AU MOMENT de l'exécution du tool cible —
    l'invariant : le seam `current_org` doit voir l'org de l'appel PENDANT run."""
    def __init__(self, name):
        super().__init__(name, result=_tool_result({"ok": True}))
        self.org_during_run = None

    async def run(self, arguments):
        from oto_mcp import session_org
        self.org_during_run = session_org.current_call_org()
        return self._result


def test_dispatch_pins_org_during_run_then_resets(oto_call_fn, monkeypatch):
    from oto_mcp import call_axes, session_org
    monkeypatch.setattr(redaction, "_resolve_field_filter", lambda _s: FieldFilter())

    async def _fake_guard(org):  # court-circuite la garde DB (appartenance réelle)
        return 167
    monkeypatch.setattr(call_axes, "resolve_org_guarded", _fake_guard)

    target = _OrgCapturingTool("zoho_records")
    assert session_org.current_call_org() is None            # propre avant
    _call(oto_call_fn, [target], name="zoho_records",
          arguments={"module": "Contacts"}, org=167)

    assert target.org_during_run == 167                      # le tool a vu l'org 167
    assert session_org.current_call_org() is None            # reset après (pas de fuite)


def test_dispatch_without_org_leaves_context_clean(oto_call_fn, monkeypatch):
    from oto_mcp import session_org
    monkeypatch.setattr(redaction, "_resolve_field_filter", lambda _s: FieldFilter())
    target = _OrgCapturingTool("zoho_records")
    _call(oto_call_fn, [target], name="zoho_records", arguments={})
    assert target.org_during_run is None                     # aucune org sans org=
    assert session_org.current_call_org() is None


def test_dispatch_org_refused_raises_before_run(oto_call_fn, monkeypatch):
    """Org non-membre : la garde lève un McpError PROPRE avant le dispatch → run
    jamais appelé, contexte inchangé (parité stricte avec l'axe plat `org=`)."""
    from oto_mcp import call_axes, session_org
    from mcp.types import ErrorData, INVALID_PARAMS

    async def _reject(org):
        raise McpError(ErrorData(code=INVALID_PARAMS, message="pas membre"))
    monkeypatch.setattr(call_axes, "resolve_org_guarded", _reject)

    target = _OrgCapturingTool("zoho_records")
    with pytest.raises(McpError):
        _call(oto_call_fn, [target], name="zoho_records", arguments={}, org=999)
    assert target.org_during_run is None                     # dispatch jamais atteint
    assert session_org.current_call_org() is None
