"""Suppression du perso : org perso dédiée (marquée `personal_of`) + migration des
ressources `owner_type='user'` vers l'org perso. On monkeypatche les seams DB.
"""
import oto_mcp.db as db
import oto_mcp.org_store as org_store


# ── ensure_personal_org : idempotence + activation ───────────────────────────
def test_returns_existing_keeps_active(monkeypatch):
    monkeypatch.setattr(org_store, "get_personal_org", lambda sub: 9)
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: 3)   # déjà une org active
    seen = []
    monkeypatch.setattr(org_store, "set_active_org", lambda s, o: seen.append((s, o)))
    assert org_store.ensure_personal_org("u1") == 9
    assert seen == []                                                 # ne change pas l'active


def test_sets_active_when_none(monkeypatch):
    monkeypatch.setattr(org_store, "get_personal_org", lambda sub: 9)
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: None)
    seen = []
    monkeypatch.setattr(org_store, "set_active_org", lambda s, o: seen.append((s, o)))
    assert org_store.ensure_personal_org("u1") == 9
    assert seen == [("u1", 9)]                                        # perso devient maison


def test_creates_when_no_personal(monkeypatch):
    monkeypatch.setattr(org_store, "get_personal_org", lambda sub: None)
    monkeypatch.setattr(org_store, "_reclaim_or_create_personal", lambda sub, e, n: 42)
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: None)
    monkeypatch.setattr(org_store, "set_active_org", lambda s, o: None)
    assert org_store.ensure_personal_org("u1", email="a@x.co") == 42


# ── _reclaim_or_create_personal : reclaim sûr vs création ────────────────────
class _Conn:
    def __init__(self, reclaim_row):
        self._row = reclaim_row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        is_reclaim = "SELECT o.id FROM orgs o" in sql
        row = self._row if is_reclaim else None
        return type("R", (), {"fetchone": lambda s: row})()


def test_reclaims_sole_org(monkeypatch):
    monkeypatch.setattr(org_store, "_connect", lambda: _Conn({"id": 7}))
    monkeypatch.setattr(org_store, "create_org",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ne doit PAS créer")))
    assert org_store._reclaim_or_create_personal("u1", None, None) == 7


def test_creates_fresh_when_no_reclaim(monkeypatch):
    monkeypatch.setattr(org_store, "_connect", lambda: _Conn(None))   # rien à réclamer
    rec = {"create": [], "member": []}
    monkeypatch.setattr(org_store, "create_org",
                        lambda name, created_by=None: rec["create"].append((name, created_by)) or 42)
    monkeypatch.setattr(org_store, "add_org_member",
                        lambda oid, sub, org_role="org_member": rec["member"].append((oid, sub, org_role)))
    assert org_store._reclaim_or_create_personal("u1", "a@x.co", "Alice") == 42
    assert rec["create"] == [("Alice", "u1")] and rec["member"] == [(42, "u1", "org_admin")]


class _RecConn:
    """Enregistre le SQL exécuté (branche create : pas de reclaim)."""
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        return type("R", (), {"fetchone": lambda s: None})()


def test_create_releases_archived_personal_slot(monkeypatch):
    """Régression 2026-07-01 : une org perso ARCHIVÉE détient encore le slot
    unique `personal_of` → le create branch doit le libérer AVANT de marquer la
    nouvelle, sinon UniqueViolation en boucle à chaque boot."""
    conn = _RecConn()
    monkeypatch.setattr(org_store, "_connect", lambda: conn)
    monkeypatch.setattr(org_store, "create_org", lambda name, created_by=None: 42)
    monkeypatch.setattr(org_store, "add_org_member", lambda oid, sub, org_role="org_member": None)
    monkeypatch.setattr(org_store, "seed_for_org", lambda *a, **k: None, raising=False)
    org_store._reclaim_or_create_personal("u1", "a@x.co", "Alice")
    updates = [s for s in conn.sql if s[0].startswith("UPDATE orgs SET personal_of")]
    # 1) libération du slot archivé (par sub), 2) marquage de la nouvelle (par id)
    assert updates[0] == ("UPDATE orgs SET personal_of = NULL WHERE personal_of = %s "
                          "AND archived_at IS NOT NULL", ("u1",))
    assert updates[1] == ("UPDATE orgs SET personal_of = %s WHERE id = %s", ("u1", 42))


# ── backfill : migration des ressources user-owned ──────────────────────────
class _Users:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        rows = self._rows
        return type("R", (), {"fetchall": lambda s: rows})()


def test_backfill_migrates_user_owned(monkeypatch):
    monkeypatch.setattr(org_store, "_connect", lambda: _Users([{"sub": "u1", "email": "a@x.co", "name": "A"}]))
    monkeypatch.setattr(org_store, "ensure_personal_org", lambda sub, e, n: 42)
    rep = {"ds": [], "pj": []}
    monkeypatch.setattr(db, "list_datastore_namespaces_for_owners", lambda owners: [{"id": 1}])
    monkeypatch.setattr(db, "reparent_datastore_namespace", lambda i, t, o: rep["ds"].append((i, t, o)))
    monkeypatch.setattr(db, "list_projects_for_owners", lambda owners, include_archived=False: [{"id": 5}])
    monkeypatch.setattr(db, "reparent_project", lambda i, t, o: rep["pj"].append((i, t, o)))
    c = org_store.backfill_personal_orgs()
    assert rep["ds"] == [(1, "org", "42")] and rep["pj"] == [(5, "org", "42")]
    assert c == {"users": 1, "datastores": 1, "projects": 1}
