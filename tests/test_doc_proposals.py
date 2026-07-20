"""Ship 3 (lot 3) — « les lecteurs proposent » (modif ET création) + inbox.

Chirurgie du dispatch : une proposition de CRÉATION (doc_id NULL) est atteignable
(routée avant le gate doc_id) ; l'acceptation branche create_doc vs update_doc ;
un viewer (lecture sans écriture) qui crée obtient une proposition. Inbox = deux
voies. Logique de capacité, db stubée.
"""
import pytest

from oto_mcp.capabilities import docs as D, inbox as I
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=7)


@pytest.fixture
def seams(monkeypatch):
    rec = {"created": [], "updated": [], "resolved": [], "cr": []}
    monkeypatch.setattr(D.db, "add_doc_change_request",
                        lambda by, **k: rec["cr"].append((by, k)) or {"id": 9, "status": "pending", **k})
    monkeypatch.setattr(D.db, "create_doc",
                        lambda pid, title, **k: rec["created"].append((pid, title, k)) or 55)
    monkeypatch.setattr(D.db, "update_doc",
                        lambda did, **k: rec["updated"].append((did, k)))
    monkeypatch.setattr(D.db, "resolve_doc_change_request",
                        lambda rid, st, by: rec["resolved"].append((rid, st, by)))
    monkeypatch.setattr(D.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(D.db, "get_doc_by_id",
                        lambda did: {"id": did, "project_id": 7, "title": "T", "body_md": "b", "kind": "doc"})
    return rec


def _can(monkeypatch, *, write):
    monkeypatch.setattr(D, "_can", lambda sub, pid, want: (want == "read") or write)


# ── viewer propose ───────────────────────────────────────────────────────────

def test_viewer_create_becomes_proposal(seams, monkeypatch):
    _can(monkeypatch, write=False)   # lecture seule
    out = D._doc(CTX, D.DocInput(op="create", project_id=7, title="Nouvelle page",
                                 parent_id=3, body_md="corps"))
    assert out["status"] == "proposal_created"
    assert seams["created"] == []                     # rien créé directement
    by, k = seams["cr"][0]
    assert k["project_id"] == 7 and k["proposed_parent_id"] == 3
    assert k["proposed_title"] == "Nouvelle page"


def test_writer_create_creates_directly(seams, monkeypatch):
    _can(monkeypatch, write=True)
    D._doc(CTX, D.DocInput(op="create", project_id=7, title="Page"))
    assert seams["created"] and seams["cr"] == []


def test_create_proposal_via_request_change_no_doc(seams, monkeypatch):
    _can(monkeypatch, write=False)
    out = D._doc(CTX, D.DocInput(op="request_change", project_id=7, title="Idée"))
    assert out["ok"] and seams["cr"][0][1]["project_id"] == 7


# ── résolution (le point sérieux : création atteignable) ─────────────────────

def test_resolve_accept_create_proposal(seams, monkeypatch):
    _can(monkeypatch, write=True)
    monkeypatch.setattr(D.db, "get_doc_change_request",
                        lambda rid: {"id": rid, "doc_id": None, "project_id": 7,
                                     "proposed_parent_id": 4, "proposed_kind": "doc",
                                     "proposed_title": "Créée", "proposed_body_md": "x",
                                     "status": "pending"})
    out = D._doc(CTX, D.DocInput(op="resolve_change", request_id=9, accept=True))
    assert out["accepted"] is True
    assert seams["created"][0][1] == "Créée"          # create_doc appelé
    assert seams["resolved"] == [(9, "accepted", "u1")]


def test_resolve_accept_modif_proposal(seams, monkeypatch):
    _can(monkeypatch, write=True)
    monkeypatch.setattr(D.db, "get_doc_change_request",
                        lambda rid: {"id": rid, "doc_id": 20, "project_id": None,
                                     "proposed_title": "Titre v2", "proposed_body_md": "b2",
                                     "status": "pending"})
    out = D._doc(CTX, D.DocInput(op="resolve_change", request_id=9, accept=True))
    assert out["accepted"] and seams["updated"][0][0] == 20


def test_resolve_create_parent_deleted_falls_to_root(seams, monkeypatch):
    _can(monkeypatch, write=True)
    monkeypatch.setattr(D.db, "get_doc_change_request",
                        lambda rid: {"id": rid, "doc_id": None, "project_id": 7,
                                     "proposed_parent_id": 4, "proposed_kind": "doc",
                                     "proposed_title": "X", "proposed_body_md": "",
                                     "status": "pending"})
    monkeypatch.setattr(D.db, "get_doc_by_id", lambda did: None)   # parent disparu
    D._doc(CTX, D.DocInput(op="resolve_change", request_id=9, accept=True))
    assert seams["created"][0][2]["parent_id"] is None            # rattaché racine


def test_resolve_requires_write(seams, monkeypatch):
    _can(monkeypatch, write=False)
    monkeypatch.setattr(D.db, "get_doc_change_request",
                        lambda rid: {"id": rid, "doc_id": None, "project_id": 7,
                                     "status": "pending"})
    with pytest.raises(AuthzDenied) as e:
        D._doc(CTX, D.DocInput(op="resolve_change", request_id=9, accept=True))
    assert e.value.code == "forbidden"


# ── inbox ────────────────────────────────────────────────────────────────────

def test_inbox_two_voies(monkeypatch):
    monkeypatch.setattr(I.ownership, "accessible_project_ids", lambda sub, org, want="read": [11])
    monkeypatch.setattr(I.ownership, "active_org_principals", lambda sub, org: [("user", "u1")])
    monkeypatch.setattr(I.db, "list_change_requests_by_project",
                        lambda pids, **k: [{"id": 1, "doc_id": None, "project_id": 11,
                                            "project_name": "P", "proposed_title": "Créer",
                                            "requested_by": "lea", "message": "svp"}])
    monkeypatch.setattr(I.db, "get_user", lambda sub: {"email": "u1@x.fr"})
    monkeypatch.setattr(I.org_store, "list_pending_invitations_for_email",
                        lambda email: [{"code": "abc", "org_id": 9, "org_name": "Acme"}])
    monkeypatch.setattr(I.db, "list_change_requests_by_requester",
                        lambda sub, **k: [{"id": 2, "status": "accepted", "doc_title": "D",
                                           "resolved_at": "2026-07-19"}])
    monkeypatch.setattr(I.db, "list_projects_granted_to", lambda pr: [])
    out = I._inbox(CTX, I.InboxInput())
    assert out["count"] == 2                     # 1 proposition + 1 invitation
    assert out["to_review"][0]["kind"] == "create"
    assert out["invitations"][0]["org_name"] == "Acme"
    assert out["recent"][0]["type"] == "proposal_resolved"


def test_inbox_no_org_empty_never_400(monkeypatch):
    monkeypatch.setattr(I.db, "get_user", lambda sub: {"email": ""})
    monkeypatch.setattr(I.db, "list_change_requests_by_requester", lambda sub, **k: [])
    monkeypatch.setattr(I.org_store, "list_pending_invitations_for_email", lambda e: [])
    out = I._inbox(ResolvedCtx(sub="u1", org_id=None), I.InboxInput())
    assert out == {"to_review": [], "invitations": [], "recent": [], "count": 0}
