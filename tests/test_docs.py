"""Capacité `oto_doc` — pages markdown arborescentes d'un projet (incrément 3).

Les Docs héritent de l'accès du PROJET (ownership.can_access sur le projet) ; on
monkeypatche db + ownership.
"""
import pytest

from oto_mcp.capabilities import docs as D
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=None)
DOC = {"id": 3, "project_id": 7, "parent_id": None, "title": "Page", "body_md": "x",
       "kind": "doc", "created_at": "2026-06-30", "updated_at": "2026-06-30"}


@pytest.fixture
def seams(monkeypatch):
    rec = {"create": [], "update": [], "delete": [], "move": []}
    monkeypatch.setattr(D.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(D.db, "get_doc_by_id", lambda i: dict(DOC, id=i) if i in (3, 9) else None)
    monkeypatch.setattr(D.db, "create_doc",
                        lambda pid, title, parent_id=None, body_md="", kind="doc", created_by=None, description=None:
                        rec["create"].append((pid, title, parent_id, kind, created_by)) or 3)
    monkeypatch.setattr(D.db, "list_docs_for_project", lambda pid: [DOC])
    monkeypatch.setattr(D.db, "update_doc",
                        lambda did, title=None, body_md=None, kind=None, edited_by=None, description=None: rec["update"].append((did, title, body_md, kind, edited_by)))
    monkeypatch.setattr(D.db, "list_doc_revisions",
                        lambda did, limit=50: [{"id": 1, "title": "v0", "body_md": "old", "edited_by": "u1", "created_at": "2026-06-30"}])
    monkeypatch.setattr(D.db, "delete_doc", lambda did: rec["delete"].append(did))
    monkeypatch.setattr(D.db, "move_doc", lambda did, p, position=None: rec["move"].append((did, p)))
    monkeypatch.setattr(D.db, "log_project_activity", lambda *a, **k: None)
    # gap #4b — demandes de modif
    rec["cr_add"], rec["cr_resolve"] = [], []
    monkeypatch.setattr(D.db, "add_doc_change_request",
                        lambda by, *, doc_id=None, project_id=None, proposed_parent_id=None,
                        proposed_kind=None, proposed_title=None, proposed_body_md="", message=None:
                        rec["cr_add"].append((doc_id, by, proposed_title, proposed_body_md, message))
                        or {"id": 5, "status": "pending"})
    monkeypatch.setattr(D.db, "list_doc_change_requests",
                        lambda did, only_pending=True: [{"id": 5, "proposed_body_md": "new", "status": "pending"}])
    monkeypatch.setattr(D.db, "get_doc_change_request",
                        lambda rid: {"id": rid, "doc_id": 3, "status": "pending",
                                     "proposed_title": "T", "proposed_body_md": "new"})
    monkeypatch.setattr(D.db, "resolve_doc_change_request",
                        lambda rid, status, by: rec["cr_resolve"].append((rid, status, by)))
    rec["set_public"] = []
    monkeypatch.setattr(D.db, "set_doc_public",
                        lambda did, public: rec["set_public"].append((did, public)) or ("tok123" if public else None))
    return rec


def test_set_public_on(seams):
    out = D._doc(CTX, D.DocInput(op="set_public", doc_id=3, public=True))
    assert seams["set_public"] == [(3, True)]
    assert out["public"] is True and out["public_url"].endswith("/p/d/tok123")


def test_set_public_off(seams):
    out = D._doc(CTX, D.DocInput(op="set_public", doc_id=3, public=False))
    assert seams["set_public"] == [(3, False)]
    assert out["public"] is False and out["public_url"] is None


def test_create(seams):
    out = D._doc(CTX, D.DocInput(op="create", project_id=7, title=" Page "))
    assert seams["create"] == [(7, "Page", None, "doc", "u1")]
    assert out["id"] == 3


def test_create_forbidden(seams, monkeypatch):
    monkeypatch.setattr(D.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied) as e:
        D._doc(CTX, D.DocInput(op="create", project_id=7, title="X"))
    assert e.value.code == "forbidden"


def test_create_missing_title(seams):
    with pytest.raises(AuthzDenied) as e:
        D._doc(CTX, D.DocInput(op="create", project_id=7, title="  "))
    assert e.value.code == "missing_title"


def test_list(seams):
    out = D._doc(CTX, D.DocInput(op="list", project_id=7))
    assert out["project_id"] == 7 and [d["id"] for d in out["docs"]] == [3]


def test_get_unknown(seams):
    with pytest.raises(AuthzDenied) as e:
        D._doc(CTX, D.DocInput(op="get", doc_id=999))
    assert e.value.code == "unknown_doc"


def test_update(seams):
    D._doc(CTX, D.DocInput(op="update", doc_id=3, body_md="new"))
    assert seams["update"] == [(3, None, "new", None, "u1")]   # edited_by = ctx.sub


def test_revisions(seams):
    out = D._doc(CTX, D.DocInput(op="revisions", doc_id=3))
    assert out["doc_id"] == 3 and out["revisions"][0]["title"] == "v0"


def test_delete(seams):
    out = D._doc(CTX, D.DocInput(op="delete", doc_id=3))
    assert seams["delete"] == [3] and out["deleted"] is True


def test_move_self_parent_rejected(seams):
    with pytest.raises(AuthzDenied) as e:
        D._doc(CTX, D.DocInput(op="move", doc_id=3, parent_id=3))
    assert e.value.code == "bad_parent"


def test_move_top_level(seams):
    D._doc(CTX, D.DocInput(op="move", doc_id=3, parent_id=None))
    assert seams["move"] == [(3, None)]


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next((c for c in CAPABILITIES if c.key == "me.doc"), None)
    assert cap is not None and cap.mcp == "oto_doc" and cap.rest.path == "/api/me/docs"


def test_request_change_with_read_only(seams, monkeypatch):
    # Lecture seule (can_access read True, write False) → la demande passe quand même.
    monkeypatch.setattr(D.ownership, "can_access",
                        lambda sub, t, rid, want="read": want == "read")
    out = D._doc(CTX, D.DocInput(op="request_change", doc_id=3, body_md="new", message="svp"))
    assert out["ok"] is True
    assert seams["cr_add"] == [(3, "u1", None, "new", "svp")]


def test_list_changes_needs_write(seams, monkeypatch):
    monkeypatch.setattr(D.ownership, "can_access", lambda sub, t, rid, want="read": want == "read")
    with pytest.raises(AuthzDenied) as e:
        D._doc(CTX, D.DocInput(op="list_changes", doc_id=3))
    assert e.value.code == "forbidden"


def test_resolve_change_accept_applies(seams):
    out = D._doc(CTX, D.DocInput(op="resolve_change", doc_id=3, request_id=5, accept=True))
    assert out["accepted"] is True
    assert seams["update"] == [(3, "T", "new", None, "u1")]      # contenu proposé appliqué
    assert seams["cr_resolve"] == [(5, "accepted", "u1")]


def test_resolve_change_reject(seams):
    D._doc(CTX, D.DocInput(op="resolve_change", doc_id=3, request_id=5, accept=False))
    assert seams["update"] == []                                  # rien appliqué
    assert seams["cr_resolve"] == [(5, "rejected", "u1")]
