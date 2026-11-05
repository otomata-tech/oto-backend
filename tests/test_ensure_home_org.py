"""`ensure_home_org` : tout compte a une org active, et plus JAMAIS d'org perso
(05/11/2026) — l'org posée à un compte sans aucune org est une org ordinaire,
marquée `auto_created`. On monkeypatche les seams DB.
"""
import oto_mcp.db as db
import oto_mcp.org_store as org_store
from oto_mcp.org_store import personal


# ── ensure_home_org : idempotence + activation ───────────────────────────
def test_returns_active_keeps_it(monkeypatch):
    monkeypatch.setattr(org_store, "home_org", lambda sub: 3)          # déjà une org active
    seen = []
    monkeypatch.setattr(org_store, "set_active_org", lambda s, o: seen.append((s, o)))
    monkeypatch.setattr(org_store, "create_org",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ne doit PAS créer")))
    assert org_store.ensure_home_org("u1") == 3
    assert seen == []                                                 # ne change pas l'active


def test_sets_oldest_live_org_active_when_none(monkeypatch):
    monkeypatch.setattr(org_store, "home_org", lambda sub: None)
    monkeypatch.setattr(org_store, "list_orgs_for_user",
                        lambda sub: [{"org_id": 9}, {"org_id": 12}])
    seen = []
    monkeypatch.setattr(org_store, "set_active_org", lambda s, o: seen.append((s, o)))
    monkeypatch.setattr(org_store, "create_org",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ne doit PAS créer")))
    assert org_store.ensure_home_org("u1") == 9
    assert seen == [("u1", 9)]                                        # la plus ancienne


class _RecConn:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        return type("R", (), {"fetchone": lambda s: None})()


def test_creates_a_plain_org_when_none_at_all(monkeypatch):
    conn = _RecConn()
    monkeypatch.setattr(org_store, "_connect", lambda: conn)
    monkeypatch.setattr(org_store, "home_org", lambda sub: None)
    monkeypatch.setattr(org_store, "list_orgs_for_user", lambda sub: [])
    rec = {"create": [], "member": [], "active": []}
    monkeypatch.setattr(org_store, "create_org",
                        lambda name, created_by=None: rec["create"].append((name, created_by)) or 42)
    monkeypatch.setattr(org_store, "add_org_member",
                        lambda oid, sub, org_role="org_member", *, actor: rec["member"].append((oid, sub, org_role)))
    monkeypatch.setattr(org_store, "set_active_org", lambda s, o: rec["active"].append((s, o)))
    from oto_mcp import discovery
    monkeypatch.setattr(discovery, "seed_for_org", lambda *a, **k: None)
    assert org_store.ensure_home_org("u1", email="alice@acme.io", name="Alice") == 42
    assert rec["create"] == [("Acme", "u1")]                          # le domaine pro
    assert rec["member"] == [(42, "u1", "org_admin")] and rec["active"] == [("u1", 42)]
    sql = [q for q, _ in conn.sql]
    assert ("UPDATE orgs SET auto_created = TRUE WHERE id = %s") in sql
    assert not any("personal_of" in q for q in sql), "plus jamais d'étiquette perso"


def test_le_nom_vient_du_domaine_pro_sinon_de_la_personne():
    assert personal._home_label("a@acme.io", "Alice") == "Acme"
    assert personal._home_label("a@gmail.com", "Alice") == "Alice"
    assert personal._home_label("bob.martin@gmail.com", None) == "bob.martin"
    assert personal._home_label(None, None) == "Mon org"


def test_aucune_org_n_est_perso():
    assert org_store.is_personal_org(1) is False


# ── backfill : garantit une org active, SANS toucher aux ressources ─────────
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


def test_backfill_ne_touche_plus_aux_ressources(monkeypatch):
    """Le backfill de boot NE DOIT PLUS reparenter les ressources user-owned.

    Régression 2026-07-28 : rejoué à chaque boot, il déplaçait tout projet PERSO
    (scope membre ADR 0030 §8) vers l'org perso de son auteur → le projet quittait
    l'org de travail et « disparaissait » de la liste de l'utilisateur."""
    monkeypatch.setattr(org_store, "_connect", lambda: _Users([{"sub": "u1", "email": "a@x.co", "name": "A"}]))
    monkeypatch.setattr(org_store, "ensure_home_org", lambda sub, e, n: 42)
    monkeypatch.setattr(org_store, "list_orgs_for_user", lambda sub: [])
    rep = []
    monkeypatch.setattr(db, "list_datastores_for_owners", lambda owners: [{"id": 1}])
    monkeypatch.setattr(db, "reparent_datastore", lambda *a, **k: rep.append(a))
    monkeypatch.setattr(db, "list_projects_for_owners", lambda owners, include_archived=False: [{"id": 5}])
    monkeypatch.setattr(db, "reparent_project", lambda *a, **k: rep.append(a))
    c = org_store.backfill_personal_orgs()
    assert rep == [], "le backfill de boot ne doit reparenter AUCUNE ressource"
    assert c == {"users": 1}
