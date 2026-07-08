"""oto_resource (ADR 0030) généralisé au type `project` : list/get type-aware
(transfer/share/unshare étaient déjà génériques via le seam ownership).
"""
import pytest

from oto_mcp import ownership
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=None)
PROW = {"id": 7, "name": "Proj", "owner_type": "user", "owner_id": "u1",
        "archived_at": None, "created_at": "2026-06-30"}


def _wire(monkeypatch):
    monkeypatch.setattr(R.access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(R.ownership, "accessor_scope", lambda sub: ownership.AccessorScope(sub, [], []))
    monkeypatch.setattr(R.roles, "is_org_admin", lambda sub, oid: False)
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "u1@x.co"})
    # transfert = re-gardé sur can_transfer dans le handler (ADR 0048) — l'acteur possède PROW.
    monkeypatch.setattr(R.ownership, "can_transfer", lambda sub, rt, rid: True)


def test_project_is_supported():
    assert "project" in R._OPS


def test_list_projects(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "list_projects_for_owners", lambda owners: [PROW])
    out = R._resources(CTX, R.ResourceInput(op="list", resource_type="project"))
    assert out["resource_type"] == "project"
    r = out["resources"][0]
    assert r["resource_id"] == "7" and r["name"] == "Proj" and r["owner_label"] == "u1@x.co"


def test_get_project_with_grants(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_project_by_id", lambda i: PROW if i == 7 else None)
    monkeypatch.setattr(R.ownership, "list_grants", lambda rt, rid: [])
    out = R._resources(CTX, R.ResourceInput(op="get", resource_type="project", resource_id="7"))
    assert out["name"] == "Proj" and out["grants"] == []


def test_transfer_routes_generically(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_user_by_email", lambda e: {"sub": "u2", "email": e})
    seen = {}
    monkeypatch.setattr(R.ownership, "transfer",
                        lambda rt, rid, ot, oid: seen.update(rt=rt, rid=rid, ot=ot, oid=oid))
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_email="u2@x.co"))
    assert seen == {"rt": "project", "rid": "7", "ot": "user", "oid": "u2"} and out["ok"]


def test_transfer_to_own_org(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.roles, "is_org_member", lambda sub, oid: oid == 35)
    monkeypatch.setattr(R.org_store, "get_org", lambda oid: {"name": "movinmotion"})
    seen = {}
    monkeypatch.setattr(R.ownership, "transfer",
                        lambda rt, rid, ot, oid: seen.update(rt=rt, rid=rid, ot=ot, oid=oid))
    out = R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                            resource_id="7", new_owner_org=35))
    assert seen == {"rt": "project", "rid": "7", "ot": "org", "oid": "35"}
    assert out["ok"] and out["new_owner"] == "movinmotion"


def test_transfer_to_org_requires_membership(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.roles, "is_org_member", lambda sub, oid: False)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, R.ResourceInput(op="transfer", resource_type="project",
                                          resource_id="7", new_owner_org=99))
    assert e.value.code == "not_org_member"


def test_unknown_type(monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, R.ResourceInput(op="list", resource_type="nope"))
    assert e.value.code == "unsupported_resource_type"


# ── ADR 0048 : « Partager » unifié (audience × rôle) ──────────────────────────

def test_share_role_manager_grants_manager(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_user_by_email", lambda e: {"sub": "u2", "email": e})
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "u2@x.co"})
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda *a, **k: True)
    seen = {}
    monkeypatch.setattr(R.ownership, "grant",
                        lambda rt, rid, pt, pid, perm=None, granted_by=None, role=None:
                        seen.update(rt=rt, pid=pid, role=role, perm=perm))
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", email="u2@x.co", role="manager"))
    assert seen["role"] == "manager" and out["role"] == "manager"
    assert out["permission"] == "write"   # manager ⇒ write (plan contenu)


def test_share_legacy_permission_maps_to_role(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_user_by_email", lambda e: {"sub": "u2", "email": e})
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "u2@x.co"})
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda *a, **k: True)
    seen = {}
    monkeypatch.setattr(R.ownership, "grant",
                        lambda rt, rid, pt, pid, perm=None, granted_by=None, role=None:
                        seen.update(role=role))
    R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                      resource_id="7", email="u2@x.co", permission="read"))
    assert seen["role"] == "viewer"   # read → viewer (rétro-compat)


def test_share_audience_public_publishes(monkeypatch):
    _wire(monkeypatch)
    row = {"id": 7, "name": "Proj", "owner_type": "org", "owner_id": "42",
           "mcp_tools": ["fr_search"], "mcp_access": "off"}
    monkeypatch.setattr(R.db, "get_project_by_id", lambda i: row)
    from oto_mcp.capabilities import projects as P
    seen = {}
    monkeypatch.setattr(P, "publish_project_mcp",
                        lambda sub, r, **kw: seen.update(sub=sub, **kw) or {"ok": True})
    out = R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                            resource_id="7", audience="public"))
    assert seen["access_mode"] == "anonymous" and seen["mcp_tools"] == ["fr_search"]
    assert out["ok"]


def test_share_audience_private_unpublishes(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_project_by_id", lambda i: {"id": 7})
    from oto_mcp.capabilities import projects as P
    seen = {}
    monkeypatch.setattr(P, "unpublish_project_mcp",
                        lambda sub, pid: seen.update(sub=sub, pid=pid) or {"ok": True})
    R._resources(CTX, R.ResourceInput(op="share", resource_type="project",
                                      resource_id="7", audience="private"))
    assert seen["pid"] == 7


def test_share_audience_public_rejected_for_datastore(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, R.ResourceInput(op="share", resource_type="datastore_namespace",
                                          resource_id="7", audience="secret"))
    assert e.value.code == "publication_unsupported"
