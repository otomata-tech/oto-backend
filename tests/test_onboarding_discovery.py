"""Onboarding = un projet « Découverte » (ADR 0032 §7 B5c).

`ensure_discovery_project` est idempotent : crée le projet d'accueil une fois,
le réutilise ensuite. On monkeypatche db/org_store (pas de DB).
"""
import oto_mcp.tools.onboarding as OB


def _wire(monkeypatch, *, profile):
    rec = {"created": [], "set_id": [], "archived": []}
    state = {"discovery_project_id": profile.get("discovery_project_id"),
             "onboarded": profile.get("onboarded", False), "profile": {}}
    monkeypatch.setattr(OB.db, "get_account_profile", lambda sub: dict(state))
    monkeypatch.setattr(OB.db, "get_project_by_id",
                        lambda pid: profile.get("project_row") if pid == profile.get("discovery_project_id") else None)
    monkeypatch.setattr(OB.org_store, "ensure_personal_org", lambda sub: 77)
    def create_project(ot, oid, name, brief, created_by=None):
        rec["created"].append((ot, oid, name, created_by)); return 555
    monkeypatch.setattr(OB.db, "create_project", create_project)
    monkeypatch.setattr(OB.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(OB.db, "set_discovery_project_id",
                        lambda sub, pid: rec["set_id"].append((sub, pid)))
    monkeypatch.setattr(OB.db, "archive_project", lambda pid: rec["archived"].append(pid))
    return rec


def test_creates_discovery_project_when_absent(monkeypatch):
    rec = _wire(monkeypatch, profile={"discovery_project_id": None})
    pid = OB.ensure_discovery_project("u1")
    assert pid == 555
    assert rec["created"] == [("org", "77", OB._DISCOVERY_PROJECT_NAME, "u1")]  # org perso
    assert rec["set_id"] == [("u1", 555)]


def test_reuses_existing_live_project(monkeypatch):
    rec = _wire(monkeypatch, profile={"discovery_project_id": 42,
                                      "project_row": {"id": 42, "archived_at": None}})
    pid = OB.ensure_discovery_project("u1")
    assert pid == 42 and rec["created"] == []   # pas de re-création


def test_recreates_if_archived(monkeypatch):
    # Le projet mémorisé a été archivé → on en recrée un.
    rec = _wire(monkeypatch, profile={"discovery_project_id": 42,
                                      "project_row": {"id": 42, "archived_at": "2026-06-30"}})
    pid = OB.ensure_discovery_project("u1")
    assert pid == 555 and rec["created"]


def test_best_effort_on_failure(monkeypatch):
    monkeypatch.setattr(OB.db, "get_account_profile", lambda sub: (_ for _ in ()).throw(RuntimeError("db down")))
    assert OB.ensure_discovery_project("u1") is None   # jamais d'exception remontée
