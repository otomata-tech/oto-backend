"""Régression #110 : l'écho `_org` d'une réponse MCP reflète l'org EFFECTIVE
APRÈS le handler, pas l'org résolue à l'autz avant lui.

`oto_use_org` bascule l'override de session DANS son handler ; l'écho `_org`
était calculé depuis `ctx.org_id` (figé à l'autz) → il montrait l'org d'AVANT le
switch (réponse trompeuse `{active_org: 83, _org: {id: 2}}`), faisant croire que
la bascule avait échoué.
"""
import asyncio

from pydantic import BaseModel

from oto_mcp import access, org_store
from oto_mcp.capabilities import _mcp_adapter
from oto_mcp.capabilities._types import Capability, ResolvedCtx


class _NoInput(BaseModel):
    pass


def _make_switch_cap():
    # authz fige org_id=2 (org d'avant) ; le handler « bascule » vers 83.
    def _authz(raw, inp):
        return ResolvedCtx(sub=raw.sub or "u", org_id=2)

    def _handler(ctx, inp):
        return {"active_org": 83, "name": "Ferme Solaire"}

    return Capability(
        key="test.switch", handler=_handler, Input=_NoInput,
        authz=_authz, mcp="test_switch", refresh_visibility=False,
    )


def test_org_echo_reflects_post_handler_org(monkeypatch):
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u")
    # current_org lit l'override POSÉ par le handler → org effective = 83.
    monkeypatch.setattr(access, "current_org", lambda sub: 83)
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"name": f"org{oid}"})

    tool = _mcp_adapter._make_tool(_make_switch_cap())
    result = asyncio.run(tool())

    assert result["active_org"] == 83
    assert result["_org"]["id"] == 83, "l'écho doit refléter l'org APRÈS le switch, pas ctx.org_id (2)"


def test_org_echo_falls_back_when_effective_none(monkeypatch):
    """current_org None (clear/perso) → repli sur ctx.org_id, pas de crash."""
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u")
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"name": f"org{oid}"})

    tool = _mcp_adapter._make_tool(_make_switch_cap())
    result = asyncio.run(tool())
    assert result["_org"]["id"] == 2, "repli sur ctx.org_id quand l'org effective est None"
