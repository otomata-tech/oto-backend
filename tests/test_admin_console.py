"""Console admin consolidée (ADR 0009, *_op) : routage des op vers les handlers
de domaine réutilisés + validation des champs requis (AuthzDenied propre).

On monkeypatch les handlers de domaine pour vérifier que chaque op route vers le
bon, et que l'Input consolidé exige les champs nécessaires par op.
"""
import pytest

from oto_mcp.capabilities import admin_console as ac
from oto_mcp.capabilities import (
    access_admin, orgs_admin, orgs_members, orgs_reads, users_admin)
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="admin", org_id=1)


def _tag(name):
    return lambda *a, **k: {"called": name}


# ── oto_admin_org ────────────────────────────────────────────────────────────
def test_org_routes(monkeypatch):
    monkeypatch.setattr(orgs_admin, "_create_org", _tag("create"))
    monkeypatch.setattr(orgs_admin, "_archive_org", _tag("archive"))
    monkeypatch.setattr(orgs_reads, "_list_all_orgs", _tag("list"))
    monkeypatch.setattr(orgs_reads, "_org_detail", _tag("get"))
    assert ac._org(CTX, ac.OrgAdminInput(op="create", name="Acme"))["called"] == "create"
    assert ac._org(CTX, ac.OrgAdminInput(op="archive", org_id=5))["called"] == "archive"
    assert ac._org(CTX, ac.OrgAdminInput(op="list"))["called"] == "list"
    assert ac._org(CTX, ac.OrgAdminInput(op="get", org_id=5))["called"] == "get"


def test_org_required_fields():
    with pytest.raises(AuthzDenied) as e:
        ac._org(CTX, ac.OrgAdminInput(op="create"))
    assert e.value.code == "missing_name"
    with pytest.raises(AuthzDenied) as e:
        ac._org(CTX, ac.OrgAdminInput(op="archive"))
    assert e.value.code == "missing_org"


# ── oto_admin_org_member ─────────────────────────────────────────────────────
def test_org_member_routes(monkeypatch):
    monkeypatch.setattr(orgs_members, "_add_member", _tag("add"))
    monkeypatch.setattr(orgs_members, "_remove_member", _tag("remove"))
    monkeypatch.setattr(orgs_members, "_set_member_role", _tag("set_role"))
    monkeypatch.setattr(orgs_members, "_resolve_target", lambda t: "sub_x")
    monkeypatch.setattr(orgs_reads, "_members", lambda oid: ["m"])
    assert ac._org_member(CTX, ac.OrgMemberAdminInput(op="add", org_id=1, target="a@b.co"))["called"] == "add"
    assert ac._org_member(CTX, ac.OrgMemberAdminInput(op="remove", org_id=1, target="x"))["called"] == "remove"
    assert ac._org_member(CTX, ac.OrgMemberAdminInput(
        op="set_role", org_id=1, target="x", role="org_admin"))["called"] == "set_role"
    assert ac._org_member(CTX, ac.OrgMemberAdminInput(op="list", org_id=1)) == {"org_id": 1, "members": ["m"]}


def test_org_member_required_fields():
    with pytest.raises(AuthzDenied) as e:
        ac._org_member(CTX, ac.OrgMemberAdminInput(op="add", org_id=1))
    assert e.value.code == "missing_target"
    with pytest.raises(AuthzDenied) as e:
        ac._org_member(CTX, ac.OrgMemberAdminInput(op="set_role", org_id=1, target="x"))
    assert e.value.code == "missing_role"


# ── oto_admin_user ───────────────────────────────────────────────────────────
def test_user_routes(monkeypatch):
    monkeypatch.setattr(users_admin, "_list_users", _tag("list"))
    monkeypatch.setattr(users_admin, "_user_detail", _tag("get"))
    monkeypatch.setattr(users_admin, "_set_role", _tag("set_role"))
    assert ac._user(CTX, ac.UserAdminInput(op="list"))["called"] == "list"
    assert ac._user(CTX, ac.UserAdminInput(op="get", target="a@b.co"))["called"] == "get"
    assert ac._user(CTX, ac.UserAdminInput(op="set_role", target="x", role="admin"))["called"] == "set_role"


# ── oto_admin_access ─────────────────────────────────────────────────────────
def test_access_routes(monkeypatch):
    monkeypatch.setattr(access_admin, "_list_waitlist", _tag("waitlist"))
    monkeypatch.setattr(access_admin, "_grant_access", _tag("grant"))
    monkeypatch.setattr(access_admin, "_reject_access", _tag("reject"))
    assert ac._access(CTX, ac.AccessAdminInput(op="waitlist"))["called"] == "waitlist"
    assert ac._access(CTX, ac.AccessAdminInput(op="grant", sub="s"))["called"] == "grant"
    with pytest.raises(AuthzDenied) as e:
        ac._access(CTX, ac.AccessAdminInput(op="grant"))
    assert e.value.code == "missing_sub"


# ── oto_admin_key_grant ──────────────────────────────────────────────────────
def test_key_grant_routes(monkeypatch):
    monkeypatch.setattr(users_admin, "_grant_key", _tag("user_grant"))
    monkeypatch.setattr(users_admin, "_revoke_key", _tag("user_revoke"))
    monkeypatch.setattr(users_admin, "_grant_org_key", _tag("org_grant"))
    monkeypatch.setattr(users_admin, "_revoke_org_key", _tag("org_revoke"))
    assert ac._key_grant(CTX, ac.KeyGrantInput(
        op="grant", scope="user", target="x", provider="apollo"))["called"] == "user_grant"
    assert ac._key_grant(CTX, ac.KeyGrantInput(
        op="grant", scope="org", org_id=2, provider="apollo"))["called"] == "org_grant"
    with pytest.raises(AuthzDenied) as e:
        ac._key_grant(CTX, ac.KeyGrantInput(op="grant", scope="user", target="x"))
    assert e.value.code == "missing_provider"


def test_key_grant_list_never_reveals_secret(monkeypatch):
    # ADR 0044 §F : list depuis les instances scope PLATFORM (provider, label, set_at), 0 secret.
    monkeypatch.setattr(ac.credentials_store, "list_platform_credentials", lambda provider=None: [
        {"provider": "apollo", "label": "otomata-apollo", "set_at": "2026-07-03 00:00:00"},
    ])
    out = ac._key_grant(CTX, ac.KeyGrantInput(op="list"))
    assert out["count"] == 1
    key = out["keys"][0]
    assert key == {"provider": "apollo", "label": "otomata-apollo", "set_at": "2026-07-03 00:00:00"}
    assert "api_key" not in key  # le secret ne transite JAMAIS dans le contexte LLM


def test_key_grant_missing_scope(monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        ac._key_grant(CTX, ac.KeyGrantInput(op="grant"))
    assert e.value.code == "missing_scope"
