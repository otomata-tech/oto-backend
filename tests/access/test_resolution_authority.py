"""L'exécution et les diagnostics suivent la même autorité, sans consommer en lecture."""
from oto_mcp.mcp_errors import McpError
from types import SimpleNamespace

import pytest

from oto_mcp import access, credentials_store, db, group_store, org_store, providers
from oto_mcp import session_org, status_hints, tenant_vault
from oto_mcp.access import cascade, chain_resolution, chain_shadow, resolve, scope, tenant_budget
from oto_mcp.connectors import readiness


@pytest.fixture
def vault(monkeypatch):
    monkeypatch.setenv("OTO_L7_DECIDE", "chain")
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    rows = {("group", "3"): "GROUP_KEY"}
    monkeypatch.setattr(access, "current_org", lambda sub: 7)  # external Unipile caller
    monkeypatch.setattr(scope, "current_org", lambda sub: 7)  # internal resolution
    monkeypatch.setattr(scope, "current_group", lambda sub: 99)
    monkeypatch.setattr(scope, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(scope, "project_pinned_instance", lambda p: None)
    monkeypatch.setattr(scope, "project_pinned_identity", lambda p: None)
    monkeypatch.setattr(session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access.rbac, "require_connector_access", lambda *a: None)
    monkeypatch.setattr(access.rbac, "rbac_denied_connectors", lambda *a: set())
    monkeypatch.setattr(access.rbac, "group_rbac_denied_connectors", lambda *a: set())
    monkeypatch.setattr(cascade, "_is_multi_account", lambda *a: False)
    monkeypatch.setattr(cascade, "personal_instance_org", lambda *a, **k: None)
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: (et, eid) in rows)
    monkeypatch.setattr(credentials_store, "get_credential",
                        lambda et, eid, p, account="": rows.get((et, eid)))
    monkeypatch.setattr(credentials_store, "instance_suspended", lambda *a, **k: False)
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(credentials_store, "list_credentials", lambda *a: [])
    monkeypatch.setattr(credentials_store, "credential_health", lambda *a: None)
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": 3}])
    monkeypatch.setattr(group_store, "has_group_secret",
                        lambda gid, p: ("group", str(gid)) in rows)
    monkeypatch.setattr(group_store, "get_group_secret",
                        lambda gid, p, account="": rows.get(("group", str(gid))))
    monkeypatch.setattr(org_store, "has_org_secret",
                        lambda oid, p: ("org", str(oid)) in rows)
    monkeypatch.setattr(org_store, "get_org_secret",
                        lambda oid, p, account="": rows.get(("org", str(oid))))
    monkeypatch.setattr(tenant_vault, "rung_tenant", lambda sub: None)
    monkeypatch.setattr(db, "get_usage_today", lambda *a: 0)
    monkeypatch.setattr(db, "usage_today_map", lambda *a: {})
    monkeypatch.setattr(cascade, "group_secret_map", lambda groups: {3: {"serper"}})
    monkeypatch.setattr(cascade, "preloaded_presence_probe",
                        lambda *a, **k: cascade.PRESENCE_PROBE)
    monkeypatch.setattr(status_hints, "has_hook", lambda p: False)
    monkeypatch.setattr(status_hints, "pending_action", lambda *a: None)
    return rows


def test_inactive_team_agrees_in_execution_status_and_readiness(vault, monkeypatch):
    monkeypatch.setattr(db, "KEY_PROVIDERS", ("serper",))
    monkeypatch.setattr(providers, "REGISTRY", {"serper": providers.REGISTRY["serper"]})
    assert access.resolve_credential("serper", sub="u").mode == "group"
    assert access.credential_mode_for("u", "serper", org=7, group=99) == "group"
    assert access.status_for("u", org=7, group=99)["providers"]["serper"]["mode"] == "group"
    assert readiness.diagnose("u", "serper", org=7, group=99) is None


def test_health_follows_the_same_winning_team(vault, monkeypatch):
    seen = []
    monkeypatch.setattr(credentials_store, "credential_health",
                        lambda *args: seen.append(args) or "invalid_grant")
    assert access.credential_rejection_for("u", "serper", org=7, group=99) == "invalid_grant"
    assert seen == [("group", "3", "serper", "")]


def test_explicit_subject_context_does_not_read_requesters_group(vault, monkeypatch):
    def wrong_context(*a):
        raise AssertionError("requester group was read")
    monkeypatch.setattr(scope, "current_group", wrong_context)
    assert access.credential_mode_for("subject", "serper", org=7, group=None) == "group"


@pytest.mark.parametrize("owner,name", [
    (credentials_store, "has_credential"),
    (group_store, "list_groups_for_user"),
    (group_store, "has_group_secret"),
    (org_store, "has_org_secret"),
])
def test_failed_lookup_does_not_select_another_identity(vault, monkeypatch, owner, name):
    vault.clear()
    def unavailable(*a, **k):
        raise RuntimeError("vault unavailable")
    monkeypatch.setattr(owner, name, unavailable)
    with pytest.raises(RuntimeError, match="vault unavailable"):
        access.resolve_credential("serper", sub="u")


def test_failed_observer_does_not_change_legacy_execution(vault, monkeypatch):
    monkeypatch.setenv("OTO_L7_DECIDE", "legacy")
    monkeypatch.setenv("OTO_L7_SHADOW", "1")
    vault[("org", "7")] = "ORG_KEY"
    def broken_observation(*a, **k):
        raise RuntimeError("groups unavailable to observer")
    monkeypatch.setattr(group_store, "list_groups_for_user", broken_observation)
    assert access.resolve_credential("serper", sub="u").key == "ORG_KEY"


def test_connection_does_not_spend_tenant_budget(vault, monkeypatch):
    win = cascade.CascadeRung("tenant", "tenant", "acme", "TENANT_KEY")
    monkeypatch.setattr(chain_shadow, "barreau_gagnant", lambda *a, **k: win)
    spends = []
    monkeypatch.setattr(tenant_budget, "enforce", lambda *a: spends.append(a))
    assert access.resolve_credential("unipile", sub="acme:u", check_usage=False).key == "TENANT_KEY"
    assert spends == []
    access.resolve_credential("unipile", sub="acme:u")
    assert spends == [("acme", "unipile", 7)]


def test_connection_does_not_apply_execution_quota(vault, monkeypatch):
    win = cascade.CascadeRung("platform", "platform", "env",
                              {"label": "env", "secret": "KEY", "daily_quota": 1})
    monkeypatch.setattr(chain_shadow, "barreau_gagnant", lambda *a, **k: win)
    reads = []
    monkeypatch.setattr(resolve, "_win_quota", lambda *a: reads.append(a) or (1, 1))
    assert access.resolve_credential("unipile", sub="u", check_usage=False).key == "KEY"
    assert reads == []
    with pytest.raises(McpError):
        access.resolve_credential("unipile", sub="u", emit_on_failure=False)
    assert len(reads) == 1


def test_connection_does_not_bypass_identity_error(vault, monkeypatch):
    from mcp.types import ErrorData, INVALID_PARAMS
    def ambiguous(*a, **k):
        raise McpError(ErrorData(code=INVALID_PARAMS, message="ambiguous account"))
    monkeypatch.setattr(chain_shadow, "barreau_gagnant", ambiguous)
    with pytest.raises(McpError):
        access.resolve_credential("unipile", sub="u", check_usage=False, emit_on_failure=False)


def test_channel_config_reads_credential_owner_not_channel(monkeypatch):
    seen = []
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda *a: seen.append(a) or {"meta": {"dsn": "api.example.test"}})
    rc = access.ResolvedCredential("whatsapp", "KEY", False, "group", "group", "3", "eu")
    assert rc.config["dsn"] == "api.example.test"
    assert seen == [("group", "3", "unipile", "eu")]


@pytest.mark.parametrize("authority", ["legacy", "chain"])
def test_suspended_member_never_beats_team(vault, monkeypatch, authority):
    monkeypatch.setenv("OTO_L7_DECIDE", authority)
    monkeypatch.setattr(scope, "current_group", lambda sub: 3)
    vault[("member", "7:u")] = "SUSPENDED_KEY"
    monkeypatch.setattr(credentials_store, "instance_suspended",
                        lambda et, *a, **k: et == "member")
    assert access.resolve_credential("serper", sub="u").key == "GROUP_KEY"


@pytest.mark.parametrize("authority", ["legacy", "chain"])
def test_named_account_crosses_missing_rung_but_never_changes_name(vault, monkeypatch, authority):
    monkeypatch.setenv("OTO_L7_DECIDE", authority)
    monkeypatch.setattr(scope, "current_group", lambda sub: 3)
    monkeypatch.setattr(cascade, "_is_multi_account", lambda *a: True)
    vault[("member", "7:u")] = "OTHER_ACCOUNT"
    monkeypatch.setattr(db, "get_member_api_key", lambda *a: None)
    monkeypatch.setattr(group_store, "get_group_secret",
                        lambda gid, p, account="": "NAMED_KEY" if account == "target" else None)
    rc = access.resolve_credential("serper", sub="u", account="target")
    assert (rc.key, rc.mode, rc.account) == ("NAMED_KEY", "group", "target")
    with pytest.raises(McpError):
        access.resolve_credential("serper", sub="u", account="missing", emit_on_failure=False)


@pytest.mark.parametrize("authority", ["legacy", "chain"])
def test_unipile_connect_uses_one_group_credential(vault, monkeypatch, authority):
    import asyncio
    from oto_mcp import unipile_connect
    from oto.tools import unipile as core

    monkeypatch.setenv("OTO_L7_DECIDE", authority)
    monkeypatch.setattr(scope, "current_group", lambda sub: 3)
    vault[("org", "7")] = "ORG_KEY"
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda et, eid, p, account="": {
                            "meta": {"dsn": "group.example.test" if et == "group" else "org.example.test"}})
    monkeypatch.setattr(db, "get_unipile_account", lambda *a: None)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda *a: [])
    monkeypatch.setattr(db, "create_unipile_pending", lambda *a, **k: None)
    def unused(*a, **k):
        raise AssertionError("BYO must not check platform seats or consumption")
    monkeypatch.setattr(access, "has_option", unused)
    monkeypatch.setattr(db, "get_org_unipile_limit", unused)
    monkeypatch.setattr(tenant_budget, "enforce", unused)
    calls, clients = [], []
    real_resolve = access.resolve_credential
    def recording_resolve(*a, **k):
        calls.append((a, k))
        return real_resolve(*a, **k)
    monkeypatch.setattr(access, "resolve_credential", recording_resolve)
    monkeypatch.setattr(core, "make_unipile_client", lambda **kw: clients.append(kw) or
                        SimpleNamespace(hosted_auth_link=lambda **k: "https://auth.example.test"))
    assert asyncio.run(unipile_connect.hosted_auth_url("u", "linkedin"))["url"]
    assert clients == [{"api_key": "GROUP_KEY", "dsn": "group.example.test"}]
    assert calls == [(("linkedin_unipile",), {
        "sub": "u", "check_usage": False, "emit_on_failure": False})]
