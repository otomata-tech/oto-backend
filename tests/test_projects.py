"""Capacité `oto_project` — CRUD de la couche Projet (owned resource ADR 0030).

Handler sync ; on monkeypatche db/ownership/roles (les seams), pas de DB.
"""
import types

import pytest

from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=None)
ROW = {"id": 7, "owner_type": "user", "owner_id": "u1", "name": "Proj", "brief_md": "b",
       "created_by": "u1", "archived_at": None, "created_at": "2026-06-30", "updated_at": "2026-06-30"}


@pytest.fixture
def seams(monkeypatch):
    rec = {"create": [], "update": [], "archive": []}
    monkeypatch.setattr(P.db, "create_project",
                        lambda ot, oid, name, brief, created_by=None: rec["create"].append((ot, oid, name, brief, created_by)) or 7)
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(ROW, id=pid) if pid == 7 else None)
    monkeypatch.setattr(P.db, "list_projects_for_owners", lambda owners: [ROW])
    monkeypatch.setattr(P.db, "update_project",
                        lambda pid, name=None, brief_md=None: rec["update"].append((pid, name, brief_md)))
    monkeypatch.setattr(P.db, "archive_project", lambda pid: rec["archive"].append(pid))
    rec["link"] = []
    rec["unlink"] = []
    monkeypatch.setattr(P.db, "add_project_link",
                        lambda pid, tt, tr, label=None: rec["link"].append((pid, tt, tr, label)))
    monkeypatch.setattr(P.db, "remove_project_link",
                        lambda pid, tt, tr: rec["unlink"].append((pid, tt, tr)) or 1)
    monkeypatch.setattr(P.db, "list_project_links",
                        lambda pid: [{"target_type": "tableau", "target_ref": "7", "label": "Leads"}])
    monkeypatch.setattr(P.ownership, "accessor_scope",
                        lambda sub: types.SimpleNamespace(owner_pairs=lambda: [("user", sub)]))
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.ownership, "can_govern", lambda sub, t, rid: True)
    monkeypatch.setattr(P.roles, "is_org_member", lambda sub, oid: True)
    monkeypatch.setattr(P.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(P.db, "list_project_activity",
                        lambda pid, limit=50: [{"sub": "u1", "action": "project.create",
                                                "detail": "Proj", "created_at": "2026-06-30"}])
    return rec


def test_create_perso(seams):
    out = P._project(CTX, P.ProjectInput(op="create", name="  Proj  ", brief_md="b"))
    assert seams["create"] == [("user", "u1", "Proj", "b", "u1")]   # owner=sub, name trimé
    assert out["id"] == 7 and out["name"] == "Proj"


def test_create_org_requires_membership(seams, monkeypatch):
    monkeypatch.setattr(P.roles, "is_org_member", lambda sub, oid: False)
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="create", name="X", owner_type="org", owner_id="5"))
    assert e.value.code == "forbidden"


def test_create_org_ok(seams):
    P._project(CTX, P.ProjectInput(op="create", name="X", owner_type="org", owner_id="5"))
    assert seams["create"][0][:2] == ("org", "5")


def test_create_missing_name(seams):
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="create", name="   "))
    assert e.value.code == "missing_name"


def test_list(seams):
    out = P._project(CTX, P.ProjectInput(op="list"))
    assert [p["id"] for p in out["projects"]] == [7]


def test_get_forbidden(seams, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="get", project_id=7))
    assert e.value.code == "forbidden" and e.value.status == 403


def test_get_unknown(seams):
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="get", project_id=999))
    assert e.value.code == "unknown_project" and e.value.status == 404


def test_update(seams):
    P._project(CTX, P.ProjectInput(op="update", project_id=7, name="New"))
    assert seams["update"] == [(7, "New", None)]


def test_archive_needs_govern(seams, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_govern", lambda sub, t, rid: False)
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="archive", project_id=7))
    assert e.value.code == "forbidden"


def test_archive_ok(seams):
    out = P._project(CTX, P.ProjectInput(op="archive", project_id=7))
    assert out == {"ok": True, "id": 7, "archived": True} and seams["archive"] == [7]


def test_get_includes_links(seams):
    out = P._project(CTX, P.ProjectInput(op="get", project_id=7))
    assert out["id"] == 7
    assert out["links"] == [{"target_type": "tableau", "target_ref": "7", "label": "Leads"}]


def test_link(seams):
    out = P._project(CTX, P.ProjectInput(op="link", project_id=7,
                                         target_type="tableau", target_ref="7", label="Leads"))
    assert seams["link"] == [(7, "tableau", "7", "Leads")]
    assert out["ok"] is True and out["links"]


def test_link_missing_target(seams):
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="link", project_id=7, target_type="tableau"))
    assert e.value.code == "missing_target"


def test_link_forbidden_without_write(seams, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="link", project_id=7, target_type="base", target_ref="kb1"))
    assert e.value.code == "forbidden"


def test_unlink(seams):
    P._project(CTX, P.ProjectInput(op="unlink", project_id=7, target_type="tableau", target_ref="7"))
    assert seams["unlink"] == [(7, "tableau", "7")]


def test_activity(seams):
    out = P._project(CTX, P.ProjectInput(op="activity", project_id=7))
    assert out["id"] == 7 and out["activity"][0]["action"] == "project.create"


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next((c for c in CAPABILITIES if c.key == "me.project"), None)
    assert cap is not None and cap.mcp == "oto_project"
    assert cap.rest is not None and cap.rest.path == "/api/me/projects"
