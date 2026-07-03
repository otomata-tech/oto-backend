"""Régression #108 (vécu prod 2026-07-04) : l'adaptateur MCP ne doit PAS retirer `org`
des kwargs quand la capacité DÉCLARE `org` en champ métier (ex. `oto_use_org.org` = l'org
CIBLE). Le pop ne vaut que pour l'axe-contexte RÉSERVÉ (cap sans champ `org`).
"""
import asyncio

from pydantic import BaseModel

from oto_mcp.capabilities import _mcp_adapter
from oto_mcp.capabilities._types import Capability, ResolvedCtx


def _authz(raw, inp=None):                    # DB-free (le test cible le pop, pas l'autz)
    return ResolvedCtx(sub=raw.sub, org_id=None)


class _InpBusinessOrg(BaseModel):
    org: int                       # champ MÉTIER (comme UseOrgInput.org)


class _InpNoOrg(BaseModel):
    pass                           # cap org-scopée → `org` = axe réservé injecté


def _make(monkeypatch, Input, key):
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u")
    seen = {}

    def _handler(ctx, inp):
        seen["inp"] = inp
        return {"ok": True}

    cap = Capability(key=key, handler=_handler, Input=Input, authz=_authz,
                     mcp=key.replace(".", "_"))
    return _mcp_adapter._make_tool(cap), seen


def test_business_org_field_is_preserved(monkeypatch):
    tool, seen = _make(monkeypatch, _InpBusinessOrg, "t.use_org")
    asyncio.run(tool(org=167))
    assert seen["inp"].org == 167          # l'org CIBLE atteint le handler, pas droppée


def test_reserved_org_axis_is_stripped(monkeypatch):
    tool, seen = _make(monkeypatch, _InpNoOrg, "t.reads")
    # `org=` (axe injecté) est posé par le middleware puis retiré ici → Input vide OK
    asyncio.run(tool(org=99))
    assert isinstance(seen["inp"], _InpNoOrg)
