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
                        lambda pid, title, parent_id=None, body_md="", kind="doc", created_by=None:
                        rec["create"].append((pid, title, parent_id, kind, created_by)) or 3)
    monkeypatch.setattr(D.db, "list_docs_for_project", lambda pid: [DOC])
    monkeypatch.setattr(D.db, "update_doc",
                        lambda did, title=None, body_md=None, kind=None: rec["update"].append((did, title, body_md, kind)))
    monkeypatch.setattr(D.db, "delete_doc", lambda did: rec["delete"].append(did))
    monkeypatch.setattr(D.db, "move_doc", lambda did, p: rec["move"].append((did, p)))
    monkeypatch.setattr(D.db, "log_project_activity", lambda *a, **k: None)
    return rec


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
    assert seams["update"] == [(3, None, "new", None)]


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
