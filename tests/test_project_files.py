"""Capacité `oto_project_files` (MCP-only) — lecture des « Autre document » d'un projet.

Handler sync ; on monkeypatche les seams (db/media_store) + le gate de contexte d'org
partagé avec `projects` (ADR 0023), pas de DB ni S3.
"""
import pytest

from oto_mcp.capabilities import project_files as P
from oto_mcp.capabilities import projects as PJ
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

# Org active = propriétaire du projet (un projet n'est lisible que dans son org, ADR 0023).
CTX = ResolvedCtx(sub="u1", org_id=99)
ROW = {"id": 7, "owner_type": "org", "owner_id": "99"}


@pytest.fixture
def seams(monkeypatch):
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(ROW, id=pid) if pid == 7 else None)
    monkeypatch.setattr(P.db, "list_project_files",
                        lambda pid: [{"id": 1, "s3_key": "k/abc/file.pdf", "filename": "file.pdf",
                                      "mime": "application/pdf", "size_bytes": 10, "title": "Brief",
                                      "description": None, "summary": None, "created_at": "2026-06-30"}])
    monkeypatch.setattr(P.media_store, "presign_get", lambda key, **k: f"https://signed/{key}")
    # Gate de contexte d'org (partagé avec `projects`) : pas de grant par défaut →
    # visibilité par owner-match seul ; pas de groupe.
    monkeypatch.setattr(PJ.db, "get_resource_grant", lambda *a, **k: None)
    monkeypatch.setattr(PJ.ownership.group_store, "list_groups_for_user", lambda sub, org_id=None: [])
    return monkeypatch


def test_list_signs_and_hides_key(seams):
    out = P._project_files(CTX, P.ProjectFilesInput(op="list", project_id=7))
    f = out["files"][0]
    assert "s3_key" not in f                                   # la clé S3 ne fuite jamais
    assert f["download_url"] == "https://signed/k/abc/file.pdf"
    assert f["title"] == "Brief"


def test_unknown_project(seams):
    with pytest.raises(AuthzDenied) as e:
        P._project_files(CTX, P.ProjectFilesInput(op="list", project_id=999))
    assert e.value.code == "unknown_project" and e.value.status == 404


def test_other_org_hidden(seams, monkeypatch):
    # Docs d'un projet d'une AUTRE org sans accès : invisibles en contexte, 404 non-disclosant
    # (même gate que `oto_project op=get`).
    monkeypatch.setattr(P.db, "get_project_by_id",
                        lambda pid: dict(ROW, id=pid, owner_id="83") if pid == 7 else None)
    monkeypatch.setattr(PJ.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied) as e:
        P._project_files(CTX, P.ProjectFilesInput(op="list", project_id=7))
    assert e.value.code == "unknown_project" and e.value.status == 404


def test_cross_org_member_blocked(seams, monkeypatch):
    # Membre de l'org propriétaire mais org active ≠ → bloqué en contexte (bascule d'org).
    monkeypatch.setattr(P.db, "get_project_by_id",
                        lambda pid: dict(ROW, id=pid, owner_id="83") if pid == 7 else None)
    monkeypatch.setattr(PJ.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(PJ.org_store, "get_org", lambda oid: {"id": oid, "name": "Ferme Solaire"})
    ctx = ResolvedCtx(sub="u1", org_id=44)
    with pytest.raises(AuthzDenied) as e:
        P._project_files(ctx, P.ProjectFilesInput(op="list", project_id=7))
    assert e.value.code == "wrong_org_context" and e.value.status == 403


@pytest.fixture
def delete_seams(seams, monkeypatch):
    calls = {"deleted": [], "s3": [], "activity": []}
    monkeypatch.setattr(P.db, "get_project_file",
                        lambda fid: {"id": fid, "project_id": 7, "s3_key": "k/abc/file.pdf",
                                     "filename": "file.pdf", "title": "Brief"} if fid == 1 else None)
    monkeypatch.setattr(P.db, "delete_project_file", lambda fid: calls["deleted"].append(fid))
    monkeypatch.setattr(P.media_store, "delete_by_key", lambda key: calls["s3"].append(key))
    monkeypatch.setattr(P.db, "log_project_activity",
                        lambda pid, sub, action, detail: calls["activity"].append((pid, sub, action, detail)))
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    return calls


def test_delete_removes_row_and_object(delete_seams):
    out = P._project_files(CTX, P.ProjectFilesInput(op="delete", project_id=7, file_id=1))
    assert out == {"ok": True}
    assert delete_seams["deleted"] == [1]
    assert delete_seams["s3"] == ["k/abc/file.pdf"]              # l'objet stocké part aussi
    assert delete_seams["activity"] == [(7, "u1", "project.file_delete", "Brief")]


def test_delete_requires_file_id(delete_seams):
    with pytest.raises(AuthzDenied) as e:
        P._project_files(CTX, P.ProjectFilesInput(op="delete", project_id=7))
    assert e.value.code == "missing_file_id" and e.value.status == 400


def test_delete_unknown_or_foreign_file(delete_seams, monkeypatch):
    # Fichier inexistant, ou rattaché à un AUTRE projet que celui de l'appel : 404.
    monkeypatch.setattr(P.db, "get_project_file",
                        lambda fid: {"id": fid, "project_id": 8, "s3_key": "k"} if fid == 1 else None)
    for fid in (1, 999):
        with pytest.raises(AuthzDenied) as e:
            P._project_files(CTX, P.ProjectFilesInput(op="delete", project_id=7, file_id=fid))
        assert e.value.code == "unknown_file" and e.value.status == 404
    assert delete_seams["deleted"] == []


def test_delete_needs_write_access(delete_seams, monkeypatch):
    # Lecture OK (le gate de contexte passe) mais pas d'accès write → 403, rien détruit.
    monkeypatch.setattr(P.ownership, "can_access",
                        lambda sub, t, rid, want="read": want == "read")
    with pytest.raises(AuthzDenied) as e:
        P._project_files(CTX, P.ProjectFilesInput(op="delete", project_id=7, file_id=1))
    assert e.value.code == "forbidden" and e.value.status == 403
    assert delete_seams["deleted"] == [] and delete_seams["s3"] == []


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next((c for c in CAPABILITIES if c.key == "me.project_files"), None)
    assert cap is not None and cap.mcp == "oto_project_files" and cap.rest is None
