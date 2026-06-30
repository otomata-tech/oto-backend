"""Capacité « contexte agent » (otomata-private#49) — assemble les 3 couches que
Claude reçoit. Derive-only : pas d'instance FastMCP en test → la couche tools se
dégrade en `available: False` sans casser la vue.
"""
import asyncio
import types

from oto_mcp.capabilities import agent_context as ac
from oto_mcp.capabilities._types import ResolvedCtx


def test_assembles_three_layers(monkeypatch):
    # Pas d'org active, pas d'instance liée → doctrine vide + tools indispo. Compte
    # non onboarded → le bloc B (catalogue) est présent ; le bloc A toujours.
    monkeypatch.setattr(ac.tool_registry, "bound_instance", lambda: None)
    monkeypatch.setattr(ac._db, "get_account_profile", lambda sub: {"onboarded": False})
    ctx = ResolvedCtx(sub="u1", org_id=None)
    out = asyncio.run(ac._agent_context(ctx, ac.AgentContextInput()))
    assert set(out) == {"org_id", "instructions", "doctrine", "tools"}
    assert "TA boîte à outils" in out["instructions"]                 # bloc A
    assert "data_*" in out["instructions"] and "apollo_*" in out["instructions"]  # bloc B catalogue
    assert out["doctrine"]["org_id"] is None
    assert out["tools"] == {"available": False}


def test_onboarded_account_omits_catalog(monkeypatch):
    # Compte onboarded → bloc B (catalogue d'onboarding) retiré de l'injection.
    monkeypatch.setattr(ac.tool_registry, "bound_instance", lambda: None)
    monkeypatch.setattr(ac._db, "get_account_profile", lambda sub: {"onboarded": True})
    out = asyncio.run(ac._agent_context(ResolvedCtx(sub="u1", org_id=None),
                                        ac.AgentContextInput()))
    assert "TA boîte à outils" in out["instructions"]                 # bloc A reste
    assert "apollo_*" not in out["instructions"]                      # catalogue absent


def test_tools_view_groups_by_namespace(monkeypatch):
    tools = [types.SimpleNamespace(name=n) for n in
             ("fr_get", "fr_search", "fr_stock_siege", "apollo_search", "data_write")]

    class _Inst:
        async def list_tools(self, run_middleware=False):
            return tools

    monkeypatch.setattr(ac.tool_registry, "bound_instance", lambda: _Inst())

    async def _hidden(ctx, sub):
        return {"apollo_search"}   # apollo masqué (non activé pour l'org)
    monkeypatch.setattr(ac.session_visibility, "compute_hidden_tools", _hidden)

    view = asyncio.run(ac._tools_view(ResolvedCtx(sub="u1", org_id=7)))
    assert view["available"] is True
    assert view["total_visible"] == 4 and view["total_hidden"] == 1
    by = {n["namespace"]: n for n in view["namespaces"]}
    assert by["fr"] == {"namespace": "fr", "visible": 3, "total": 3}
    assert by["apollo"] == {"namespace": "apollo", "visible": 0, "total": 1}
    assert by["data"]["visible"] == 1


def test_tools_view_degrades_on_error(monkeypatch):
    class _Inst:
        async def list_tools(self, run_middleware=False):
            raise RuntimeError("boom")
    monkeypatch.setattr(ac.tool_registry, "bound_instance", lambda: _Inst())
    view = asyncio.run(ac._tools_view(ResolvedCtx(sub="u1", org_id=7)))
    assert view == {"available": False}


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    keys = {c.key for c in CAPABILITIES}
    assert "me.agent_context" in keys
    cap = next(c for c in CAPABILITIES if c.key == "me.agent_context")
    assert cap.rest is not None and cap.rest.path == "/api/me/agent-context"
    assert cap.mcp is None   # REST-only (l'agent a déjà ce contexte)
