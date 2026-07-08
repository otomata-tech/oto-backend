"""Auto-retrait d'une org (`me.leave_org`, self-service SUB_ONLY).

Gardes du handler `_leave_org` : org inconnue → 404, espace perso → 409, non-membre
→ 404, dernier org_admin → 409, sinon retrait. On monkeypatche `org_store` (pas de PG).
"""
from types import SimpleNamespace

import pytest

import oto_mcp.capabilities.orgs_members as om
from oto_mcp.capabilities._types import AuthzDenied


def _ctx(sub="u1"):
    return SimpleNamespace(sub=sub)


def _patch(monkeypatch, *, org=True, personal=False, role="org_member",
           admins=2, removed=True):
    monkeypatch.setattr(om.org_store, "get_org", lambda oid: {"id": oid} if org else None)
    monkeypatch.setattr(om.org_store, "is_personal_org", lambda oid: personal)
    monkeypatch.setattr(om.org_store, "get_org_role", lambda oid, sub: role)
    monkeypatch.setattr(
        om.org_store, "list_org_members",
        lambda oid: [{"org_role": "org_admin"} for _ in range(admins)]
        + [{"org_role": "org_member"}],
    )
    calls = {}
    def _remove(oid, sub):
        calls["removed"] = (oid, sub)
        return removed
    monkeypatch.setattr(om.org_store, "remove_org_member", _remove)
    return calls


def test_leave_ok(monkeypatch):
    calls = _patch(monkeypatch, role="org_member")
    out = om._leave_org(_ctx("u1"), om.LeaveOrgInput(org_id=7))
    assert out == {"ok": True, "org_id": 7, "left": True}
    assert calls["removed"] == (7, "u1")


def test_leave_admin_not_last_ok(monkeypatch):
    _patch(monkeypatch, role="org_admin", admins=2)
    out = om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert out["left"] is True


def test_leave_unknown_org(monkeypatch):
    _patch(monkeypatch, org=False)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "unknown_org"


def test_leave_personal_refused(monkeypatch):
    _patch(monkeypatch, personal=True)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "personal_org"


def test_leave_not_member(monkeypatch):
    _patch(monkeypatch, role=None)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "not_a_member"


def test_leave_last_admin_refused(monkeypatch):
    _patch(monkeypatch, role="org_admin", admins=1)
    with pytest.raises(AuthzDenied) as e:
        om._leave_org(_ctx(), om.LeaveOrgInput(org_id=7))
    assert e.value.code == "last_org_admin"
