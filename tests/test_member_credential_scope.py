"""ADR 0033 — credential membre scopé (sub, org) : fin du BYO org-agnostique.

Trois invariants :
1. la clé d'un membre est keyée (sub, org) dans le coffre — posée dans l'org A,
   introuvable depuis l'org B (`db.get_member_api_key`) ;
2. la cascade `resolve_credential` cherche la clé membre dans l'ORG DE CONTEXTE
   (seam `current_org`) — changer d'org change la clé résolue, et une org sans
   rien lève (plus de repli org-agnostique) ;
3. le backfill de migration `('user', sub)` → `('member', '{home}:{sub}')`
   re-chiffre les familles non-oauth, saute oauth / indéchiffrable / sans-maison.
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access, credentials_store
from oto_mcp.db import keys as db_keys


# --- 1. grain coffre : (sub, org) ------------------------------------------------

def test_member_key_does_not_cross_orgs(monkeypatch):
    vault: dict = {}
    monkeypatch.setattr(db_keys, "upsert_user", lambda sub: None)
    monkeypatch.setattr(
        credentials_store, "set_credential",
        lambda et, eid, con, secret, **kw: vault.__setitem__((et, eid, con), secret))
    monkeypatch.setattr(
        credentials_store, "get_credential",
        lambda et, eid, con, account="": vault.get((et, eid, con)))
    monkeypatch.setattr(
        credentials_store, "has_credential",
        lambda et, eid, con, account=None: (et, eid, con) in vault)

    db_keys.set_member_api_key("u1", 1, "pennylane", "K-ORG1")
    assert vault == {("member", "1:u1", "pennylane"): "K-ORG1"}

    assert db_keys.get_member_api_key("u1", 1, "pennylane") == "K-ORG1"
    assert db_keys.get_member_api_key("u1", 2, "pennylane") is None   # autre org
    assert db_keys.get_member_api_key("u2", 1, "pennylane") is None   # autre sub
    assert db_keys.has_member_api_key("u1", 2, "pennylane") is False
    # org de contexte introuvable (défensif) → jamais un repli org-agnostique.
    assert db_keys.get_member_api_key("u1", None, "pennylane") is None
    assert db_keys.has_member_api_key("u1", None, "pennylane") is False


# --- 2. cascade : la clé membre suit l'org de contexte ---------------------------

def _wire_resolution(monkeypatch, *, org_of_key: int, current: int):
    monkeypatch.setattr(access, "require_connector_access", lambda p, s=None: None)
    monkeypatch.setattr(access, "current_org", lambda sub: current)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(
        access.db, "get_member_api_key",
        lambda sub, org, prov: "K-MEMBER" if org == org_of_key else None)
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: None)
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)


def test_resolve_member_key_in_its_org(monkeypatch):
    _wire_resolution(monkeypatch, org_of_key=1, current=1)
    rc = access.resolve_credential("pennylane", sub="u1")
    assert rc.key == "K-MEMBER" and rc.mode == "user"
    assert rc.entity_type == "member" and rc.entity_id == "1:u1"


def test_resolve_member_key_absent_from_other_org(monkeypatch):
    # La même clé, vue depuis une AUTRE org : rien ne résout (pennylane = byo-only,
    # pas de palier plateforme) → McpError actionnable, pas la clé d'à côté.
    _wire_resolution(monkeypatch, org_of_key=1, current=2)
    with pytest.raises(McpError):
        access.resolve_credential("pennylane", sub="u1", emit_on_failure=False)


def test_credential_mode_for_scopes_third_party_org(monkeypatch):
    # État d'un tiers (org explicite) : la clé membre se cherche dans SON org,
    # jamais celle du requérant (seam acteur-scopé ADR 0023).
    seen: list = []
    monkeypatch.setattr(
        access.db, "has_member_api_key",
        lambda sub, org, prov: seen.append(org) or org == 7)
    assert access.credential_mode_for("u1", "pennylane", org=7, group=None) == "user"
    assert seen == [7]


# --- 3. backfill de migration ----------------------------------------------------

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        rows = self._rows
        class _Cur:
            def fetchall(self):
                return rows
        return _Cur()


def test_backfill_member_scope_routes_families(monkeypatch):
    rows = [
        # non-oauth avec maison → MIGRE
        {"entity_id": "u1", "connector": "pennylane", "account": "",
         "secret_enc": "enc-ok", "meta": {}, "set_by": "u1"},
        # famille oauth (google) → INTOUCHÉE (B3)
        {"entity_id": "u1", "connector": "google", "account": "a@b.c",
         "secret_enc": "enc-goog", "meta": {}, "set_by": "u1"},
        # indéchiffrable (InvalidTag pré-rotation) → SKIP, laissée en place
        {"entity_id": "u1", "connector": "serpapi", "account": "",
         "secret_enc": "enc-bad", "meta": {}, "set_by": "u1"},
        # user sans org maison → SKIP
        {"entity_id": "u9", "connector": "pennylane", "account": "",
         "secret_enc": "enc-ok", "meta": {}, "set_by": "u9"},
    ]
    upserts, deletes = [], []
    monkeypatch.setattr(credentials_store, "_connect", lambda: _FakeConn(rows))
    monkeypatch.setattr(
        credentials_store, "_upsert",
        lambda conn, et, eid, con, acct, secret, set_by, meta:
            upserts.append((et, eid, con, acct, secret)))
    monkeypatch.setattr(
        credentials_store, "_delete",
        lambda conn, et, eid, con, acct: deletes.append((et, eid, con, acct)))

    def _decrypt(enc, aad):
        if enc == "enc-bad":
            raise RuntimeError("InvalidTag")
        return f"clear:{enc}"
    monkeypatch.setattr(credentials_store.crypto, "decrypt", _decrypt)

    from oto_mcp import org_store
    monkeypatch.setattr(org_store, "get_active_org",
                        lambda sub: 5 if sub == "u1" else None)

    counts = credentials_store.backfill_member_scope()
    assert counts == {"migrated": 1, "skipped": 2}
    assert upserts == [("member", "5:u1", "pennylane", "", "clear:enc-ok")]
    assert deletes == [("user", "u1", "pennylane", "")]


# --- 4. tripwire : plus d'écriture au scope 'user' hors famille oauth -------------

_OAUTH_FAMILY_FILES = {
    # Flux OAuth dédiés — SEULS écrivains légitimes du scope ('user', sub)
    # jusqu'aux barreaux B3/B4 d'ADR 0033.
    "google.py", "memento_oauth.py", "atlassian_oauth.py", "folk_oauth.py",
}


def test_no_user_scope_credential_writes_outside_oauth_family():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "oto_mcp"
    offenders = []
    pattern = re.compile(r'set_credential\(\s*["\']user["\']')
    for f in root.rglob("*.py"):
        if f.name in _OAUTH_FAMILY_FILES:
            continue
        if pattern.search(f.read_text(encoding="utf-8")):
            offenders.append(str(f.relative_to(root)))
    assert not offenders, (
        f"écriture credential au scope 'user' (org-agnostique, ADR 0033) hors "
        f"famille oauth : {offenders} — utiliser le scope MEMBER (sub, org)")
