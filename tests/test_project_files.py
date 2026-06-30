"""Capacité `oto_project_files` (MCP-only) — lecture des « Autre document » d'un projet.

Handler sync ; on monkeypatche les seams (db/ownership/media_store), pas de DB ni S3.
"""
import pytest

from oto_mcp.capabilities import project_files as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=None)


@pytest.fixture
def seams(monkeypatch):
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: {"id": pid} if pid == 7 else None)
    monkeypatch.setattr(P.db, "list_project_files",
                        lambda pid: [{"id": 1, "s3_key": "k/abc/file.pdf", "filename": "file.pdf",
                                      "mime": "application/pdf", "size_bytes": 10, "title": "Brief",
                                      "description": None, "summary": None, "created_at": "2026-06-30"}])
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.media_store, "presign_get", lambda key, **k: f"https://signed/{key}")
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


def test_forbidden_without_read(seams, monkeypatch):
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": False)
    with pytest.raises(AuthzDenied) as e:
        P._project_files(CTX, P.ProjectFilesInput(op="list", project_id=7))
    assert e.value.code == "forbidden" and e.value.status == 403


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next((c for c in CAPABILITIES if c.key == "me.project_files"), None)
    assert cap is not None and cap.mcp == "oto_project_files" and cap.rest is None
