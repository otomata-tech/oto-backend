"""Suppression du perso : tout user a TOUJOURS une org maison.

`ensure_home_org` (idempotent : active existante → 1ʳᵉ org → création) + le backfill
boot. On monkeypatche les fonctions DB d'org_store.
"""
import oto_mcp.org_store as org_store


def _wire(monkeypatch, *, active=None, orgs=None):
    rec = {"created": [], "members": [], "active": []}
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: active)
    monkeypatch.setattr(org_store, "list_orgs_for_user", lambda sub: orgs or [])
    monkeypatch.setattr(org_store, "create_org",
                        lambda name, created_by=None: rec["created"].append((name, created_by)) or 42)
    monkeypatch.setattr(org_store, "add_org_member",
                        lambda oid, sub, org_role="org_member": rec["members"].append((oid, sub, org_role)))
    monkeypatch.setattr(org_store, "set_active_org",
                        lambda sub, oid: rec["active"].append((sub, oid)) or True)
    return rec


def test_returns_existing_active(monkeypatch):
    rec = _wire(monkeypatch, active=7)
    assert org_store.ensure_home_org("u1") == 7
    assert rec["created"] == [] and rec["active"] == []   # idempotent, rien créé


def test_activates_first_when_member_but_inactive(monkeypatch):
    rec = _wire(monkeypatch, active=None, orgs=[{"org_id": 5}])
    assert org_store.ensure_home_org("u1") == 5
    assert rec["active"] == [("u1", 5)] and rec["created"] == []


def test_creates_personal_org(monkeypatch):
    rec = _wire(monkeypatch, active=None, orgs=[])
    assert org_store.ensure_home_org("u1", email="ed@x.co", name="Edouard") == 42
    assert rec["created"] == [("Edouard", "u1")]
    assert rec["members"] == [(42, "u1", "org_admin")]
    assert rec["active"] == [("u1", 42)]


def test_label_fallbacks(monkeypatch):
    rec = _wire(monkeypatch, active=None, orgs=[])
    org_store.ensure_home_org("u1", email="ed@x.co")          # pas de name → local email
    assert rec["created"][0][0] == "ed"
    rec2 = _wire(monkeypatch, active=None, orgs=[])
    org_store.ensure_home_org("u2")                           # ni name ni email → défaut
    assert rec2["created"][0][0] == "Mon espace"


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        rows = self._rows
        return type("R", (), {"fetchall": lambda s: rows})()


def test_backfill_creates_for_orphans(monkeypatch):
    orphans = [{"sub": "a", "email": "a@x.co", "name": "A"}, {"sub": "b", "email": None, "name": None}]
    monkeypatch.setattr(org_store, "_connect", lambda: _FakeConn(orphans))
    seen = []
    monkeypatch.setattr(org_store, "ensure_home_org",
                        lambda sub, email=None, name=None: seen.append((sub, email, name)) or 1)
    assert org_store.backfill_home_orgs() == 2
    assert seen == [("a", "a@x.co", "A"), ("b", None, None)]
