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
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(ROW, id=pid) if pid in (7, 8) else None)
    monkeypatch.setattr(P.db, "list_projects_for_owners",
                        lambda owners, templates_only=False: [dict(ROW, is_template=True)] if templates_only else [ROW])
    monkeypatch.setattr(P.db, "update_project",
                        lambda pid, name=None, brief_md=None, is_template=None: rec["update"].append((pid, name, brief_md, is_template)))
    rec["copy"] = []
    monkeypatch.setattr(P.db, "duplicate_project",
                        lambda src, name, ot, oid, copied_by=None: rec["copy"].append((src, name, ot, oid, copied_by)) or 8)
    monkeypatch.setattr(P.db, "archive_project", lambda pid: rec["archive"].append(pid))
    rec["link"] = []
    rec["unlink"] = []
    monkeypatch.setattr(P.db, "add_project_link",
                        lambda pid, tt, tr, label=None, role=None, config=None: rec["link"].append((pid, tt, tr, label, role, config)))
    monkeypatch.setattr(P.db, "remove_project_link",
                        lambda pid, tt, tr: rec["unlink"].append((pid, tt, tr)) or 1)
    monkeypatch.setattr(P.db, "list_project_links",
                        lambda pid: [{"target_type": "tableau", "target_ref": "7", "label": "Leads",
                                      "role": "vivier de leads", "config": {}, "cross_project": False}])
    monkeypatch.setattr(P.ownership, "accessor_scope",
                        lambda sub: types.SimpleNamespace(owner_pairs=lambda: [("user", sub)]))
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.ownership, "can_govern", lambda sub, t, rid: True)
    monkeypatch.setattr(P.roles, "is_org_member", lambda sub, oid: True)
    monkeypatch.setattr(P.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(P.db, "list_project_activity",
                        lambda pid, limit=50: [{"sub": "u1", "action": "project.create",
                                                "detail": "Proj", "created_at": "2026-06-30"}])
    # Bracelet projet (B2.2) : session présente + record des poses/retraits.
    rec["proj"] = []
    monkeypatch.setattr(P.session_org, "current_session_id", lambda: "sess1")
    monkeypatch.setattr(P.session_org, "set_project_override",
                        lambda sid, pid: rec["proj"].append((sid, pid)))
    monkeypatch.setattr(P.session_org, "clear_project_override",
                        lambda sid: rec["proj"].append((sid, None)))
    return rec


def test_create_defaults_to_active_org(seams):
    # Suppression du perso : le défaut crée dans l'ORG ACTIVE (ctx.org_id), plus en user.
    ctx = ResolvedCtx(sub="u1", org_id=99)
    out = P._project(ctx, P.ProjectInput(op="create", name="  Proj  ", brief_md="b"))
    assert seams["create"] == [("org", "99", "Proj", "b", "u1")]     # owner=org active
    assert out["id"] == 7 and out["name"] == "Proj"


def test_create_without_active_org_rejected(seams):
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="create", name="X"))      # CTX.org_id = None
    assert e.value.code == "no_active_org"


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
    assert seams["update"] == [(7, "New", None, None)]


def test_update_publish_template_needs_govern(seams, monkeypatch):
    # Publier comme modèle = gouvernance (can_govern), pas un simple write.
    monkeypatch.setattr(P.ownership, "can_govern", lambda sub, t, rid: False)
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="update", project_id=7, is_template=True))
    assert e.value.code == "forbidden"


def test_update_publish_template_ok(seams):
    out = P._project(CTX, P.ProjectInput(op="update", project_id=7, is_template=True))
    assert seams["update"] == [(7, None, None, True)]
    assert out["is_template"] is False   # vue reflète ROW (stub) — publication persistée côté db


def test_list_templates(seams):
    out = P._project(CTX, P.ProjectInput(op="list_templates"))
    assert [p["id"] for p in out["projects"]] == [7]
    assert out["projects"][0]["is_template"] is True


def test_copy(seams):
    ctx = ResolvedCtx(sub="u1", org_id=42)
    out = P._project(ctx, P.ProjectInput(op="copy", project_id=7, name="  Ma copie  "))
    assert seams["copy"] == [(7, "Ma copie", "org", "42", "u1")]
    assert out["id"] == 8 and out["copied_from"] == 7 and "links" in out


def test_copy_needs_read(seams, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied) as e:
        P._project(ResolvedCtx(sub="u1", org_id=42), P.ProjectInput(op="copy", project_id=7, name="X"))
    assert e.value.code == "forbidden" and e.value.status == 403


def test_copy_requires_name(seams):
    with pytest.raises(AuthzDenied) as e:
        P._project(ResolvedCtx(sub="u1", org_id=42), P.ProjectInput(op="copy", project_id=7, name="  "))
    assert e.value.code == "missing_name"


def test_copy_requires_active_org(seams):
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="copy", project_id=7, name="X"))   # CTX.org_id=None
    assert e.value.code == "no_active_org"


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
    assert out["links"][0]["role"] == "vivier de leads"
    assert out["links"][0]["cross_project"] is False


def test_link(seams):
    out = P._project(CTX, P.ProjectInput(op="link", project_id=7,
                                         target_type="tableau", target_ref="7", label="Leads"))
    assert seams["link"] == [(7, "tableau", "7", "Leads", None, None)]
    assert out["ok"] is True and out["links"]


def test_link_with_role(seams):
    P._project(CTX, P.ProjectInput(op="link", project_id=7, target_type="base",
                                   target_ref="kb1", label="Ton of voice",
                                   role="charte éditoriale de référence"))
    assert seams["link"] == [(7, "base", "kb1", "Ton of voice", "charte éditoriale de référence", None)]


def test_link_connector_with_config(seams):
    # ADR 0032 §4 (B2) : surcharge connecteur préfaite — identité + instructions en prose.
    cfg = {"identity_id": "acc_1", "instructions_md": "filtrer les accords par thème mutuelle"}
    out = P._project(CTX, P.ProjectInput(op="link", project_id=7, target_type="connecteur",
                                         target_ref="fr", label="Entreprises FR", config=cfg))
    assert seams["link"] == [(7, "connecteur", "fr", "Entreprises FR", None, cfg)]
    assert out["ok"] is True


def test_link_config_defaults_none(seams):
    # `config` absent côté handler → None (la couche DB le traduit en {} à la création,
    # ou préserve l'existant via COALESCE au re-link).
    P._project(CTX, P.ProjectInput(op="link", project_id=7, target_type="connecteur", target_ref="fr"))
    assert seams["link"][0][5] is None


def test_get_link_carries_config(seams):
    out = P._project(CTX, P.ProjectInput(op="get", project_id=7))
    assert out["links"][0]["config"] == {}


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


# ── Bracelet « projet actif » (B2.2) ─────────────────────────────────────────

def test_use_project_sets_session_override(seams):
    out = P._use_project(CTX, P.UseProjectInput(project_id=7))
    assert seams["proj"] == [("sess1", 7)]
    assert out["active_project"] == 7 and out["name"] == "Proj"


def test_use_project_unknown(seams):
    with pytest.raises(AuthzDenied) as e:
        P._use_project(CTX, P.UseProjectInput(project_id=999))
    assert e.value.code == "unknown_project" and e.value.status == 404
    assert seams["proj"] == []          # rien posé sur un projet inconnu


def test_use_project_forbidden(seams, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied) as e:
        P._use_project(CTX, P.UseProjectInput(project_id=7))
    assert e.value.code == "forbidden" and e.value.status == 403


def test_use_project_requires_session(seams, monkeypatch):
    monkeypatch.setattr(P.session_org, "current_session_id", lambda: None)   # face REST
    with pytest.raises(AuthzDenied) as e:
        P._use_project(CTX, P.UseProjectInput(project_id=7))
    assert e.value.code == "no_session"


def test_use_project_returns_connector_overrides(seams, monkeypatch):
    monkeypatch.setattr(P.db, "list_project_links", lambda pid: [
        {"target_type": "connecteur", "target_ref": "fr",
         "config": {"identity_id": "acc_1", "instructions_md": "thème mutuelle"}},
        {"target_type": "connecteur", "target_ref": "google", "config": {}},  # sans surcharge → exclu
        {"target_type": "tableau", "target_ref": "9", "config": {}},
    ])
    out = P._use_project(CTX, P.UseProjectInput(project_id=7))
    assert out["connector_overrides"] == [
        {"connector": "fr", "config": {"identity_id": "acc_1", "instructions_md": "thème mutuelle"}}]


def test_clear_project(seams):
    out = P._clear_project(CTX, P.NoInput())
    assert out == {"active_project": None} and seams["proj"] == [("sess1", None)]


def test_use_clear_project_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    use = next((c for c in CAPABILITIES if c.key == "me.use_project"), None)
    clear = next((c for c in CAPABILITIES if c.key == "me.clear_project"), None)
    assert use is not None and use.mcp == "oto_use_project" and use.rest is None
    assert clear is not None and clear.mcp == "oto_clear_project" and clear.rest is None
