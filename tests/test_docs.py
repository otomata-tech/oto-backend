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
                        lambda did, title=None, body_md=None, kind=None, edited_by=None, description=None, expected_rev=None: rec["update"].append((did, title, body_md, kind, edited_by, expected_rev)))
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
    assert seams["update"] == [(3, None, "new", None, "u1", None)]   # edited_by = ctx.sub


def test_patch_replaces_only_target_section(seams, monkeypatch):
    body = "# T\n\n## A\n\nvieux A.\n\n## B\n\ngarder B.\n"
    monkeypatch.setattr(D.db, "get_doc_by_id", lambda i: dict(DOC, id=i, body_md=body))
    D._doc(CTX, D.DocInput(op="patch", doc_id=3, section="A", body_md="NEUF A."))
    # update_doc reçoit le corps COMPLET patché : A remplacé, B intact
    new_body = seams["update"][0][2]
    assert "NEUF A." in new_body and "vieux A." not in new_body and "garder B." in new_body


def test_patch_unknown_section_is_404(seams, monkeypatch):
    body = "# T\n\n## A\n\nx\n"
    monkeypatch.setattr(D.db, "get_doc_by_id", lambda i: dict(DOC, id=i, body_md=body))
    with pytest.raises(D.AuthzDenied) as ei:
        D._doc(CTX, D.DocInput(op="patch", doc_id=3, section="Zzz", body_md="x"))
    assert ei.value.status == 404 and ei.value.code == "unknown_section"


def test_patch_passes_expected_rev_for_conflict(seams, monkeypatch):
    body = "# T\n\n## A\n\nx\n"
    monkeypatch.setattr(D.db, "get_doc_by_id", lambda i: dict(DOC, id=i, body_md=body))
    D._doc(CTX, D.DocInput(op="patch", doc_id=3, section="A", body_md="y", expected_rev="r1"))
    assert seams["update"][0][5] == "r1"   # le conflit optimiste s'applique aussi au patch


def test_update_passes_expected_rev(seams):
    D._doc(CTX, D.DocInput(op="update", doc_id=3, body_md="new", expected_rev="abc123"))
    assert seams["update"][0][5] == "abc123"   # le token de conflit optimiste est transmis


def test_cr_created_notifies_admins_not_proposer(seams, monkeypatch):
    # Proposition de modif (viewer) → notifie les org_admins de l'org du projet + le
    # propriétaire user, JAMAIS le proposeur ni les simples membres (oto/#6).
    monkeypatch.setattr(D.ownership, "can_access",
                        lambda sub, t, rid, want="read": want == "read")  # lecture seule → propose
    monkeypatch.setattr(D.db, "get_project_by_id",
                        lambda pid: {"id": pid, "name": "P", "context_org_id": 7,
                                     "owner_type": "org", "owner_id": "7"})
    monkeypatch.setattr(D.org_store, "list_org_members", lambda org: [
        {"sub": "admin1", "org_role": "org_admin"},
        {"sub": "u1", "org_role": "org_admin"},      # le proposeur (CTX.sub) — exclu
        {"sub": "member1", "org_role": "org_member"},  # simple membre — exclu
    ])
    emails = {"admin1": "a1@x.fr", "u1": "prop@x.fr", "member1": "m1@x.fr"}
    monkeypatch.setattr(D.db, "get_user",
                        lambda sub: {"email": emails.get(sub), "name": sub})
    sent = []
    monkeypatch.setattr(D.email, "send_change_request_email",
                        lambda to, **k: sent.append(to) or True)
    D._doc(CTX, D.DocInput(op="request_change", doc_id=3, body_md="new"))
    assert sent == ["a1@x.fr"]   # admin1 seul ; ni le proposeur ni le membre


def test_cr_resolved_notifies_proposer(seams, monkeypatch):
    monkeypatch.setattr(D.db, "get_doc_change_request",
                        lambda rid: {"id": rid, "doc_id": 3, "project_id": 7, "status": "pending",
                                     "proposed_title": "T", "proposed_body_md": "new",
                                     "requested_by": "bob", "project_name": "P", "doc_title": "Page"})
    monkeypatch.setattr(D.db, "get_user", lambda sub: {"email": "bob@x.fr"} if sub == "bob" else {})
    got = {}
    monkeypatch.setattr(D.email, "send_change_request_resolved_email",
                        lambda to, **k: got.update(to=to, accepted=k.get("accepted")) or True)
    D._doc(CTX, D.DocInput(op="resolve_change", doc_id=3, request_id=5, accept=True))
    assert got == {"to": "bob@x.fr", "accepted": True}   # le proposeur, verdict accepté


def test_update_conflict_is_409(seams, monkeypatch):
    # Le doc a changé depuis la lecture → DocConflict → erreur actionnable 409, pas d'écrasement.
    def _boom(*a, **k):
        raise D.db.DocConflict("newrev99")
    monkeypatch.setattr(D.db, "update_doc", _boom)
    with pytest.raises(D.AuthzDenied) as ei:
        D._doc(CTX, D.DocInput(op="update", doc_id=3, body_md="x", expected_rev="stale"))
    assert ei.value.status == 409 and ei.value.code == "conflict"


def test_revisions(seams):
    out = D._doc(CTX, D.DocInput(op="revisions", doc_id=3))
    assert out["doc_id"] == 3 and out["revisions"][0]["title"] == "v0"


def test_move_to_another_project(seams, monkeypatch):
    # A4 (#6) : op=move avec to_project déplace la page + sous-arbre vers le projet cible
    # (écriture requise sur source ET cible ; cible doit exister).
    rec = {}
    monkeypatch.setattr(D.db, "get_project_by_id", lambda i: {"id": i} if i in (3, 8) else None)
    monkeypatch.setattr(D.db, "move_doc_to_project",
                        lambda did, tgt, parent=None, position=None: rec.update(
                            did=did, tgt=tgt, parent=parent) or 3)
    out = D._doc(CTX, D.DocInput(op="move", doc_id=3, to_project=8))
    assert rec == {"did": 3, "tgt": 8, "parent": None}
    assert out["moved_count"] == 3
    assert seams["move"] == []          # PAS le move intra-projet


def test_move_to_unknown_project_404(seams, monkeypatch):
    monkeypatch.setattr(D.db, "get_project_by_id", lambda i: dict(DOC, id=i) if i == 3 else None)
    with pytest.raises(D.AuthzDenied) as ei:
        D._doc(CTX, D.DocInput(op="move", doc_id=3, to_project=999))
    assert ei.value.status == 404 and ei.value.code == "unknown_project"


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
    assert seams["update"] == [(3, "T", "new", None, "u1", None)]      # contenu proposé appliqué
    assert seams["cr_resolve"] == [(5, "accepted", "u1")]


def test_resolve_change_reject(seams):
    D._doc(CTX, D.DocInput(op="resolve_change", doc_id=3, request_id=5, accept=False))
    assert seams["update"] == []                                  # rien appliqué
    assert seams["cr_resolve"] == [(5, "rejected", "u1")]
