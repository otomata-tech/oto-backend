"""ADR 0044 — partage d'instance : share_down (restreindre, allowlist deny-by-default)
+ share_side (étendre, prêt nominatif). Exerce la VRAIE logique (get_instance_sharing
mocké avec des données), pas le fail-safe sans DB."""
import types

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access, credentials_store, group_store, roles, instance_refs, db, providers
from oto_mcp.capabilities import connectors_sharing
from oto_mcp.capabilities._types import AuthzDenied


# ── _sub_matches_scopes : vocabulaire commun aux deux axes ────────────────────
def test_sub_matches_scopes_user(monkeypatch):
    assert access._sub_matches_scopes("alice", ["user:alice"]) is True
    assert access._sub_matches_scopes("alice", ["user:bob"]) is False

def test_sub_matches_scopes_org_is_everyone():
    assert access._sub_matches_scopes("whoever", ["org"]) is True

def test_sub_matches_scopes_group_membership(monkeypatch):
    monkeypatch.setattr(group_store, "is_group_member",
                        lambda sub, gid: sub == "alice" and gid == 5)
    assert access._sub_matches_scopes("alice", ["group:5"]) is True
    assert access._sub_matches_scopes("bob", ["group:5"]) is False

def test_sub_matches_scopes_empty_is_false():
    assert access._sub_matches_scopes("alice", []) is False

def test_sub_matches_scopes_ignores_malformed(monkeypatch):
    monkeypatch.setattr(group_store, "is_group_member", lambda s, g: False)
    assert access._sub_matches_scopes("alice", ["group:notanint", "user:alice"]) is True


# ── share_down : allowlist deny-by-default ────────────────────────────────────
def _mock_sharing(monkeypatch, down, side):
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, conn, acct="": (down, side))

def test_share_down_empty_is_open(monkeypatch):
    _mock_sharing(monkeypatch, [], [])
    assert access._share_down_allows("org", "35", "zoho", "alice") is True

def test_share_down_restricts_to_listed(monkeypatch):
    _mock_sharing(monkeypatch, ["user:alice"], [])
    assert access._share_down_allows("org", "35", "zoho", "alice") is True
    assert access._share_down_allows("org", "35", "zoho", "bob") is False

def test_share_down_group_scope(monkeypatch):
    _mock_sharing(monkeypatch, ["group:2"], [])
    monkeypatch.setattr(group_store, "is_group_member", lambda sub, gid: sub == "sales_guy")
    assert access._share_down_allows("org", "35", "zoho", "sales_guy") is True
    assert access._share_down_allows("org", "35", "zoho", "other") is False


# ── guard : share_down au PIN d'une instance d'org (pas de contournement) ─────
def test_guard_org_pin_respects_share_down(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    _mock_sharing(monkeypatch, ["group:2"], [])
    monkeypatch.setattr(group_store, "is_group_member", lambda sub, gid: sub == "sales_guy")
    ref = instance_refs.parse_ref(instance_refs.make_org_ref(35, "zoho"))
    # membre de l'org mais hors de l'allowlist → refus DUR au pin
    with pytest.raises(McpError, match="réservée"):
        access.guard_instance_access("intrus", ref)
    # membre listé → OK, co-pose l'org de l'instance
    assert access.guard_instance_access("sales_guy", ref) == 35


# ── guard : share_side (prêt à un pair) ───────────────────────────────────────
def test_guard_member_share_side_allows_beneficiary(monkeypatch):
    # instance de "owner" dans l'org 8, prêtée à "bob"
    _mock_sharing(monkeypatch, [], ["user:bob"])
    monkeypatch.setattr(access, "current_org", lambda sub: 99)  # org de l'APPELANT
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(8, "owner", "zoho"))
    # bob emprunte : autorisé, co-pose SON org (99), pas celle de l'owner (8)
    assert access.guard_instance_access("bob", ref) == 99

def test_guard_member_share_side_rejects_non_beneficiary(monkeypatch):
    _mock_sharing(monkeypatch, [], ["user:bob"])
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(8, "owner", "zoho"))
    with pytest.raises(McpError, match="autre membre"):
        access.guard_instance_access("carol", ref)

def test_guard_member_owner_still_works(monkeypatch):
    # le propriétaire garde le chemin owner (pas de lecture share_side nécessaire)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(8, "owner", "zoho"))
    assert access.guard_instance_access("owner", ref) == 8


# ── oto_lend_instance : write path de share_side ──────────────────────────────
def _lend_wiring(monkeypatch, *, existing_side=None, write_ok=True):
    captured = {}
    monkeypatch.setattr(providers, "connector_for_provider", lambda c: object())
    monkeypatch.setattr(access, "current_org", lambda sub: 35)
    monkeypatch.setattr(db, "get_user", lambda sub: {"sub": sub})
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, conn, acct="": ([], list(existing_side or [])))
    def _set(et, eid, conn, acct="", *, share_down=None, share_side=None):
        captured["share_side"] = share_side
        captured["eid"] = eid
        return write_ok
    monkeypatch.setattr(credentials_store, "set_instance_sharing", _set)
    return captured

def _ctx(sub="alice"):
    return types.SimpleNamespace(sub=sub)

def test_lend_adds_beneficiary(monkeypatch):
    cap = _lend_wiring(monkeypatch)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="bob")
    out = connectors_sharing._lend_instance(_ctx("alice"), inp)
    assert cap["share_side"] == ["user:bob"]
    assert cap["eid"] == "35:alice"          # ne prête QUE sa propre ligne
    assert out["lent_to"] == ["bob"] and out["revoked"] is False

def test_lend_revoke_removes(monkeypatch):
    cap = _lend_wiring(monkeypatch, existing_side=["user:bob", "user:carol"])
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="bob", revoke=True)
    out = connectors_sharing._lend_instance(_ctx("alice"), inp)
    assert cap["share_side"] == ["user:carol"]
    assert out["lent_to"] == ["carol"] and out["revoked"] is True

def test_lend_no_instance_raises(monkeypatch):
    _lend_wiring(monkeypatch, write_ok=False)  # aucune ligne à mettre à jour
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="bob")
    with pytest.raises(AuthzDenied, match="rien à prêter|Aucune instance"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)

def test_lend_self_rejected(monkeypatch):
    _lend_wiring(monkeypatch)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="alice")
    with pytest.raises(AuthzDenied, match="soi-même"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)

def test_lend_unknown_user_rejected(monkeypatch):
    _lend_wiring(monkeypatch)
    monkeypatch.setattr(db, "get_user", lambda sub: None)
    inp = connectors_sharing.LendInstanceInput(connector="zoho", to="ghost")
    with pytest.raises(AuthzDenied, match="inconnu"):
        connectors_sharing._lend_instance(_ctx("alice"), inp)
