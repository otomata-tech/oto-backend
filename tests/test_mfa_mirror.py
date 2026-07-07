"""Tests — MFA obligatoire par org (miroir « organization Logto »).

Étages :
1. `_member_subs` prend TOUS les membres (jamais filtré sur `is_active`) ;
2. `sync_members` : no-op sans miroir + réconciliation add/remove ;
3. `ensure_mirror` / `disable_mirror` : flux de provisioning ;
4. capacité `org.mfa.set` : provisionne AVANT le drapeau, fail-closed si Logto plante ;
5. enregistrement des capacités.

Tous les appels réseau (requests) sont monkeypatchés au grain des helpers du module.
"""
import pytest

from oto_mcp import mfa_mirror
from oto_mcp.capabilities import orgs_mfa
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES

ORG = 8
CTX = ResolvedCtx(sub="usr_admin", org_id=ORG, role="org_admin")


# ─── 1. _member_subs ne filtre PAS is_active ─────────────────────────────────

def test_member_subs_includes_inactive_membership(monkeypatch):
    # b a son org active AILLEURS (is_active=False sur CETTE org) mais reste membre :
    # il DOIT être dans le miroir, sinon il échappe au MFA.
    monkeypatch.setattr(mfa_mirror.org_store, "list_org_members", lambda oid: [
        {"sub": "a", "is_active": True}, {"sub": "b", "is_active": False}])
    assert mfa_mirror._member_subs(ORG) == {"a", "b"}


# ─── 2. sync_members ─────────────────────────────────────────────────────────

def test_sync_members_noop_without_mirror(monkeypatch):
    monkeypatch.setattr(mfa_mirror.org_store, "get_org_mfa",
                        lambda oid: {"require_mfa": False, "logto_org_id": None})
    # Toute tentative d'appel Logto ferait planter le test.
    for fn in ("_list_logto_members", "_add_logto_members", "_remove_logto_member"):
        monkeypatch.setattr(mfa_mirror, fn, lambda *a, **k: pytest.fail("appel Logto interdit"))
    mfa_mirror.sync_members(ORG)   # ne lève pas


def test_sync_members_reconciles(monkeypatch):
    monkeypatch.setattr(mfa_mirror.org_store, "get_org_mfa",
                        lambda oid: {"require_mfa": True, "logto_org_id": "L1"})
    monkeypatch.setattr(mfa_mirror.org_store, "list_org_members", lambda oid: [
        {"sub": "a", "is_active": True}, {"sub": "b", "is_active": False}])
    monkeypatch.setattr(mfa_mirror, "_list_logto_members", lambda lid: {"b", "c"})
    added, removed = [], []
    monkeypatch.setattr(mfa_mirror, "_add_logto_members", lambda lid, subs: added.extend(subs))
    monkeypatch.setattr(mfa_mirror, "_remove_logto_member", lambda lid, sub: removed.append(sub))
    mfa_mirror.sync_members(ORG)
    assert added == ["a"]      # want{a,b} - have{b,c}
    assert removed == ["c"]    # have{b,c} - want{a,b}


# ─── 3. ensure_mirror / disable_mirror ───────────────────────────────────────

def test_ensure_mirror_creates_when_absent(monkeypatch):
    calls = {}
    monkeypatch.setattr(mfa_mirror.org_store, "get_org_mfa",
                        lambda oid: {"require_mfa": False, "logto_org_id": None})
    monkeypatch.setattr(mfa_mirror.org_store, "get_org", lambda oid: {"name": "Acme"})

    def _fake_create(name, desc=""):
        calls["created"] = (name, desc)
        return "LNEW"
    monkeypatch.setattr(mfa_mirror, "_create_logto_org", _fake_create)
    monkeypatch.setattr(mfa_mirror.org_store, "set_org_logto_org_id",
                        lambda oid, lid: calls.setdefault("stored", lid))
    monkeypatch.setattr(mfa_mirror, "_set_mfa_required",
                        lambda lid, req: calls.setdefault("mfa", (lid, req)))
    monkeypatch.setattr(mfa_mirror, "sync_members", lambda oid: calls.setdefault("synced", oid))
    assert mfa_mirror.ensure_mirror(ORG) == "LNEW"
    assert calls["stored"] == "LNEW"
    assert calls["mfa"] == ("LNEW", True)
    assert calls["synced"] == ORG


def test_ensure_mirror_reuses_existing(monkeypatch):
    monkeypatch.setattr(mfa_mirror.org_store, "get_org_mfa",
                        lambda oid: {"require_mfa": True, "logto_org_id": "LEXIST"})
    monkeypatch.setattr(mfa_mirror, "_create_logto_org",
                        lambda *a, **k: pytest.fail("ne doit pas recréer"))
    monkeypatch.setattr(mfa_mirror, "_set_mfa_required", lambda lid, req: None)
    monkeypatch.setattr(mfa_mirror, "sync_members", lambda oid: None)
    assert mfa_mirror.ensure_mirror(ORG) == "LEXIST"


def test_disable_mirror_lowers_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(mfa_mirror.org_store, "get_org_mfa",
                        lambda oid: {"require_mfa": True, "logto_org_id": "L1"})
    monkeypatch.setattr(mfa_mirror, "_set_mfa_required",
                        lambda lid, req: seen.update(lid=lid, req=req))
    mfa_mirror.disable_mirror(ORG)
    assert seen == {"lid": "L1", "req": False}


# ─── 4. capacité org.mfa.set — pas de fail-open ──────────────────────────────

def test_set_enables_provisioning_before_flag(monkeypatch):
    order = []
    monkeypatch.setattr(orgs_mfa.org_store, "get_org", lambda oid: {"name": "Acme"})
    monkeypatch.setattr(orgs_mfa.mfa_mirror, "ensure_mirror",
                        lambda oid: order.append("provision"))
    monkeypatch.setattr(orgs_mfa.org_store, "set_org_require_mfa",
                        lambda oid, v: order.append(("flag", v)))
    out = orgs_mfa._set_org_mfa(CTX, orgs_mfa.SetOrgMfaInput(org_id=ORG, require=True))
    assert order == ["provision", ("flag", True)]   # provision AVANT le drapeau
    assert out == {"ok": True, "org_id": ORG, "require_mfa": True}


def test_set_enable_failclosed_when_logto_down(monkeypatch):
    monkeypatch.setattr(orgs_mfa.org_store, "get_org", lambda oid: {"name": "Acme"})

    def boom(oid):
        raise RuntimeError("logto down")
    monkeypatch.setattr(orgs_mfa.mfa_mirror, "ensure_mirror", boom)
    flag_set = []
    monkeypatch.setattr(orgs_mfa.org_store, "set_org_require_mfa",
                        lambda oid, v: flag_set.append(v))
    with pytest.raises(AuthzDenied) as ei:
        orgs_mfa._set_org_mfa(CTX, orgs_mfa.SetOrgMfaInput(org_id=ORG, require=True))
    assert ei.value.status == 502
    assert flag_set == []   # le drapeau n'est JAMAIS posé si le provisioning échoue


def test_set_disable_lowers_mirror_before_flag(monkeypatch):
    order = []
    monkeypatch.setattr(orgs_mfa.org_store, "get_org", lambda oid: {"name": "Acme"})
    monkeypatch.setattr(orgs_mfa.mfa_mirror, "disable_mirror",
                        lambda oid: order.append("disable"))
    monkeypatch.setattr(orgs_mfa.org_store, "set_org_require_mfa",
                        lambda oid, v: order.append(("flag", v)))
    orgs_mfa._set_org_mfa(CTX, orgs_mfa.SetOrgMfaInput(org_id=ORG, require=False))
    assert order == ["disable", ("flag", False)]


def test_set_unknown_org_404(monkeypatch):
    monkeypatch.setattr(orgs_mfa.org_store, "get_org", lambda oid: None)
    with pytest.raises(AuthzDenied) as ei:
        orgs_mfa._set_org_mfa(CTX, orgs_mfa.SetOrgMfaInput(org_id=999, require=True))
    assert ei.value.status == 404


# ─── 5. enregistrement des capacités ─────────────────────────────────────────

def test_capabilities_registered():
    by_key = {c.key: c for c in CAPABILITIES}
    assert by_key["org.mfa.get"].mcp == "oto_get_org_mfa"
    assert by_key["org.mfa.set"].mcp == "oto_set_org_mfa"
