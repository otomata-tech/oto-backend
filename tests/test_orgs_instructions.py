"""Domaine doctrine/instructions d'org migré en capacités (ADR 0009).

Couvre : préservation du contrat REST `/api/me/instructions*`, présence des outils
MCP (membre + admin), la garde anti-dérive sur le nom d'outil compté par l'usage
(le bug d'origine), le combinateur d'autz `ORG_ADMIN`, et le support des handlers
asynchrones par l'adaptateur (doctrine + manifeste).
"""
import asyncio

import pytest
from pydantic import BaseModel

from oto_mcp.capabilities import _authz, _mcp_adapter, registry
from oto_mcp.capabilities import orgs_instructions as oi
from oto_mcp.capabilities._types import AuthzDenied, Capability, RawCtx, ResolvedCtx


class _Empty(BaseModel):
    pass


# ── Contrat REST préservé (consommé par oto-dashboard) ──────────────────────
def test_member_instruction_routes_preserved():
    pairs = {(b.verb, b.path) for c in registry.caps_with_rest() for b in c.rest_bindings()}
    for vp in [
        ("GET", "/api/me/instructions"),
        ("GET", "/api/me/instructions/{slug}"),
        ("PUT", "/api/me/instructions/{slug}"),
        ("DELETE", "/api/me/instructions/{slug}"),
        ("GET", "/api/me/instructions/{slug}/versions"),
        ("GET", "/api/me/instructions/{slug}/usage"),
        ("POST", "/api/me/instructions/{slug}/revert"),
    ]:
        assert vp in pairs, vp


def test_doctrine_mcp_tools_present():
    names = {c.mcp for c in registry.caps_with_mcp()}
    for n in [
        "oto_get_doctrine", "oto_list_doctrines", "oto_set_doctrine", "oto_delete_doctrine",
        "oto_admin_get_doctrine", "oto_admin_list_doctrines",
        "oto_admin_set_doctrine", "oto_admin_delete_doctrine",
    ]:
        assert n in names, n


# ── Garde anti-dérive (le bug d'origine) ────────────────────────────────────
def test_usage_tool_name_is_a_mounted_tool():
    """L'outil interrogé par l'usage doctrine DOIT être un tool MCP réellement
    monté — sinon le filtre `tool_calls` renvoie 0 (cause du bug initial)."""
    names = {c.mcp for c in registry.caps_with_mcp()}
    assert oi._DOCTRINE_GET_TOOL in names


# ── Combinateur d'autz ORG_ADMIN (org active) ───────────────────────────────
def test_org_admin_active_combinator(monkeypatch):
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")

    monkeypatch.setattr(_authz.org_store, "get_active_org", lambda sub: None)
    with pytest.raises(AuthzDenied) as e:
        _authz.ORG_ADMIN(RawCtx(sub="u1"))
    assert e.value.status == 400 and e.value.code == "no_active_org"

    monkeypatch.setattr(_authz.org_store, "get_active_org", lambda sub: 7)
    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, oid: False)
    with pytest.raises(AuthzDenied) as e:
        _authz.ORG_ADMIN(RawCtx(sub="u1"))
    assert e.value.status == 403

    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, oid: True)
    ctx = _authz.ORG_ADMIN(RawCtx(sub="u1"))
    assert ctx.org_id == 7 and ctx.sub == "u1"


# ── Support handler asynchrone par l'adaptateur MCP ─────────────────────────
def test_mcp_adapter_awaits_async_handler(monkeypatch):
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u1")

    async def h(ctx, inp):
        return {"ok": True, "sub": ctx.sub}

    cap = Capability(
        key="t.async", handler=h, Input=_Empty,
        authz=lambda raw, inp: ResolvedCtx(sub="u1"), mcp="t_async",
    )
    tool = _mcp_adapter._make_tool(cap)
    assert asyncio.run(tool()) == {"ok": True, "sub": "u1"}
