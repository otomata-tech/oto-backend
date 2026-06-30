"""Capacité `oto_kb` — base de connaissance d'org (zone Documents, remplace Memento)."""
import pytest

from oto_mcp.capabilities import kb as K
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture
def seams(monkeypatch):
    rec = {"created": []}
    monkeypatch.setattr(K.db, "create_project",
                        lambda ot, oid, name, brief, created_by=None:
                        rec["created"].append((ot, oid, name)) or 42)
    monkeypatch.setattr(K.db, "get_project_by_id",
                        lambda pid: {"id": pid, "name": K.KB_NAME, "brief_md": K.KB_BRIEF})
    monkeypatch.setattr(K.db, "log_project_activity", lambda *a, **k: None)
    return rec


def test_get_creates_when_absent(seams, monkeypatch):
    monkeypatch.setattr(K.db, "list_projects_for_owners", lambda owners: [])
    out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    assert seams["created"] == [("org", "7", K.KB_NAME)]
    assert out["project_id"] == 42 and out["name"] == K.KB_NAME


def test_get_returns_existing(seams, monkeypatch):
    monkeypatch.setattr(K.db, "list_projects_for_owners",
                        lambda owners: [{"id": 9, "name": K.KB_NAME, "brief_md": "x"},
                                        {"id": 3, "name": "autre projet"}])
    out = K._kb(ResolvedCtx(sub="u1", org_id=7), K.KbInput(op="get"))
    assert out["project_id"] == 9 and seams["created"] == []   # pas de doublon


def test_no_active_org(seams):
    with pytest.raises(AuthzDenied) as e:
        K._kb(ResolvedCtx(sub="u1", org_id=None), K.KbInput(op="get"))
    assert e.value.code == "no_active_org"


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next((c for c in CAPABILITIES if c.key == "me.kb"), None)
    assert cap is not None and cap.mcp == "oto_kb"
