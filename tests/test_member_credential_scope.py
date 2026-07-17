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
    monkeypatch.setattr(access.db, "member_instance_suspended",
                        lambda *a, **k: False)
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
        # google (oauth mais PAS un mount) → MIGRE depuis B3, account préservé
        {"entity_id": "u1", "connector": "google", "account": "a@b.c",
         "secret_enc": "enc-goog", "meta": {"is_default": True}, "set_by": "u1"},
        # mount oauth fédéré (memento) → INTOUCHÉ (barreau ultérieur)
        {"entity_id": "u1", "connector": "memento", "account": "",
         "secret_enc": "enc-memento", "meta": {}, "set_by": "u1"},
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
    assert counts == {"migrated": 2, "skipped": 2}
    assert upserts == [("member", "5:u1", "pennylane", "", "clear:enc-ok"),
                       ("member", "5:u1", "google", "a@b.c", "clear:enc-goog")]
    assert deletes == [("user", "u1", "pennylane", ""),
                       ("user", "u1", "google", "a@b.c")]


# --- 4. tripwire : plus d'écriture au scope 'user' hors famille oauth -------------

_OAUTH_FAMILY_FILES = {
    # Mounts OAuth fédérés — SEULS écrivains légitimes du scope ('user', sub)
    # restants (barreau ultérieur d'ADR 0033). Google est passé au scope membre en B3.
    "memento_oauth.py", "atlassian_oauth.py", "folk_oauth.py",
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


# --- 5. B3/B4 : google + unipile au scope membre ----------------------------------

def test_google_state_carries_org(monkeypatch):
    # L'org du DÉMARRAGE voyage dans le state HMAC jusqu'au callback (qui vient de
    # Google, sans headers de consultation) — roundtrip + rejet des vieux formats.
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
    from oto_mcp import google_oauth
    state = google_oauth.make_state("u1", 42)
    assert google_oauth.verify_state(state) == ("u1", 42)
    # un state sans org (format pré-B3) est refusé, pas interprété org-agnostique
    import base64, hashlib, hmac as hm, json as js
    payload = js.dumps({"sub": "u1", "ts": 10**10}, separators=(",", ":")).encode()
    sig = hm.new(b"test-secret", payload, hashlib.sha256).digest()
    legacy = (base64.urlsafe_b64encode(payload).rstrip(b"=").decode() + "." +
              base64.urlsafe_b64encode(sig).rstrip(b"=").decode())
    assert google_oauth.verify_state(legacy) is None


def test_google_db_scoped_by_member_entity(monkeypatch):
    # Le grain coffre des comptes Google = ('member', '{org}:{sub}') — l'org de
    # contexte borne la liste ; org None (défensif) → aucun compte, jamais un repli.
    from oto_mcp.db import google as db_google
    calls = []
    monkeypatch.setattr(
        credentials_store, "list_accounts",
        lambda et, eid, con: calls.append((et, eid)) or [])
    assert db_google.list_google_accounts("u1", 7) == []
    assert calls == [("member", "7:u1")]
    assert db_google.list_google_accounts("u1", None) == []   # pas d'appel coffre
    assert calls == [("member", "7:u1")]
    assert db_google.get_google_oauth("u1", None) is None


def test_unipile_db_guards_org_none():
    # Les getters unipile sans org de contexte ne résolvent RIEN (jamais de repli
    # org-agnostique) — et n'ouvrent aucune connexion DB (pas de DATABASE_URL ici).
    from oto_mcp.db import unipile as db_unipile
    assert db_unipile.get_unipile_account_id("u1", None) is None
    assert db_unipile.get_unipile_account("u1", None) is None
    assert db_unipile.get_unipile_feed_synced_at("u1", None) is None
    with pytest.raises(ValueError):
        db_unipile.set_unipile_account("u1", "ACC", org_id=None)
