"""Clé d'équipe « à portée » (non active) — hint de statut + erreur actionnable.

Vécu 2026-07-16 (Zoho / movinmotion) : la clé vivait sur l'équipe sales (3 membres,
0 actif) → la cascade ne résout que le groupe ACTIF, l'user voyait « pas de clé »
sec et le drawer un faux message RBAC. Couvre :
- `access.reachable_team_key` : équipe dont le sub est membre + secret présent ;
- `access.reachable_instances` + `_reachable_hint` : les instances à portée
  (équipes membres + autres orgs) remontées dans les erreurs « rien ne résout »,
  geste de pin per-call (`group=`/`org=`/`instance=`) en tête ;
- `status_for` : miroir de cascade des providers `fields` (group/org, ex-user-only)
  + champ `team_key_group` quand mode=forbidden.
"""
import pytest

from mcp.shared.exceptions import McpError

from oto_mcp import access


SALES = {"group_id": 2, "org_id": 35, "name": "sales",
         "group_role": "group_member", "is_active": False, "joined_at": "2026-07-03"}


def _wire_reachable(monkeypatch, *, groups=(SALES,), secret_groups=(2,), orgs=()):
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: list(groups))
    monkeypatch.setattr(access.group_store, "has_group_secret",
                        lambda gid, prov: gid in secret_groups)
    monkeypatch.setattr(access.org_store, "list_orgs_for_user",
                        lambda sub: list(orgs))


# --- reachable_team_key --------------------------------------------------------

def test_reachable_team_key_finds_member_team_secret(monkeypatch):
    _wire_reachable(monkeypatch)
    assert access.reachable_team_key("u1", 35, "zoho") == {"id": 2, "name": "sales"}


def test_reachable_team_key_none_without_secret(monkeypatch):
    _wire_reachable(monkeypatch, secret_groups=())
    assert access.reachable_team_key("u1", 35, "zoho") is None


def test_reachable_team_key_none_for_non_shareable(monkeypatch):
    _wire_reachable(monkeypatch)
    # provider hors ORG_SHAREABLE_PROVIDERS (pas de palier équipe) → jamais de hint
    assert access.reachable_team_key("u1", 35, "zohodesk") is None


def test_reachable_team_key_none_without_org(monkeypatch):
    _wire_reachable(monkeypatch)
    assert access.reachable_team_key("u1", None, "zoho") is None


def test_reachable_team_key_best_effort_on_db_error(monkeypatch):
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert access.reachable_team_key("u1", 35, "zoho") is None


# --- erreur actionnable de résolution ------------------------------------------

def test_reachable_instances_lists_teams_and_other_orgs(monkeypatch):
    _wire_reachable(monkeypatch,
                    orgs=[{"org_id": 35, "name": "movinmotion"},
                          {"org_id": 167, "name": "Movinmotion Test"}])
    monkeypatch.setattr(access.org_store, "has_org_secret",
                        lambda oid, prov: oid == 167)
    monkeypatch.setattr(access.db, "has_member_api_key", lambda *a, **k: False)
    items = access.reachable_instances("u1", 35, "zoho")
    assert {"kind": "group", "id": 2, "name": "sales"} in items
    assert {"kind": "org", "id": 167, "name": "Movinmotion Test"} in items
    # l'org ambiante (35) n'est jamais listée : elle est déjà dans la cascade
    assert not any(i["kind"] == "org" and i["id"] == 35 for i in items)


def test_reachable_instances_includes_org_admin_governed_teams(monkeypatch):
    # #218 : org_admin NON membre de l'équipe sales (list_groups_for_user vide pour lui)
    # mais la clé zoho y vit → le hint DOIT quand même la lister (accès par escalade).
    from oto_mcp import roles
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])            # pas membre
    monkeypatch.setattr(access.group_store, "list_groups",
                        lambda org_id: [{"id": 2, "name": "sales"},
                                        {"id": 9, "name": "finance"}])
    monkeypatch.setattr(access.group_store, "has_group_secret",
                        lambda gid, prov: gid == 2)             # clé sur sales seule
    monkeypatch.setattr(access.org_store, "list_orgs_for_user", lambda sub: [])
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: True)
    items = access.reachable_instances("admin", 35, "zoho")
    assert {"kind": "group", "id": 2, "name": "sales"} in items
    assert not any(i["id"] == 9 for i in items)                 # finance sans clé, absent


def test_reachable_instances_non_admin_stays_membership_only(monkeypatch):
    # Non-admin, non membre : pas d'escalade → aucune équipe remontée (pas de fuite).
    from oto_mcp import roles
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])
    monkeypatch.setattr(access.group_store, "list_groups",
                        lambda org_id: [{"id": 2, "name": "sales"}])
    monkeypatch.setattr(access.group_store, "has_group_secret", lambda gid, prov: gid == 2)
    monkeypatch.setattr(access.org_store, "list_orgs_for_user", lambda sub: [])
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: False)
    assert access.reachable_instances("u1", 35, "zoho") == []


def test_resolution_failure_mentions_reachable_team(monkeypatch):
    monkeypatch.setattr(access, "require_connector_access", lambda *a, **k: None)
    monkeypatch.setattr(access.session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda prov: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 35)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    monkeypatch.setattr(access.credentials_store, "list_accounts", lambda *a, **k: [])
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: None)
    _wire_reachable(monkeypatch)
    with pytest.raises(McpError) as e:
        access._resolve_credential_impl("zoho", "byo", "u1")
    # le geste per-call (jeton d'appel) d'abord, le ref d'instance en repli
    assert "group=2" in str(e.value)
    assert "sales" in str(e.value)
    assert "instance=group:2:zoho" in str(e.value)


def test_resolution_failure_lists_other_org(monkeypatch):
    monkeypatch.setattr(access, "require_connector_access", lambda *a, **k: None)
    monkeypatch.setattr(access.session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda prov: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 35)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    monkeypatch.setattr(access.credentials_store, "list_accounts", lambda *a, **k: [])
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: None)
    _wire_reachable(monkeypatch, secret_groups=(),
                    orgs=[{"org_id": 167, "name": "Movinmotion Test"}])
    monkeypatch.setattr(access.org_store, "has_org_secret",
                        lambda oid, prov: oid == 167)
    monkeypatch.setattr(access.db, "has_member_api_key", lambda *a, **k: False)
    with pytest.raises(McpError) as e:
        access._resolve_credential_impl("zoho", "byo", "u1")
    assert "org=167" in str(e.value)
    assert "Movinmotion Test" in str(e.value)


def test_resolution_failure_plain_without_team(monkeypatch):
    monkeypatch.setattr(access, "require_connector_access", lambda *a, **k: None)
    monkeypatch.setattr(access.session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda prov: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 35)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    monkeypatch.setattr(access.credentials_store, "list_accounts", lambda *a, **k: [])
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: None)
    _wire_reachable(monkeypatch, secret_groups=())
    with pytest.raises(McpError) as e:
        access._resolve_credential_impl("zoho", "byo", "u1")
    assert "à portée" not in str(e.value)


# --- status_for : miroir cascade des providers fields + team_key_group ---------

def _wire_status(monkeypatch, *, member=False, group_secret=False, org_secret=False,
                 active_group=None):
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access, "personal_instance_org", lambda *a, **k: None)
    monkeypatch.setattr(access.db, "has_member_api_key",
                        lambda sub, org, prov: member)
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, prov: 0)
    monkeypatch.setattr(access, "_platform_grant_meta", lambda *a, **k: None)
    monkeypatch.setattr(access.group_store, "has_group_secret",
                        lambda gid, prov: group_secret)
    monkeypatch.setattr(access.org_store, "has_org_secret",
                        lambda oid, prov: org_secret)
    monkeypatch.setattr(access.credentials_store, "credential_status",
                        lambda *a, **k: None)
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])
    return lambda: access.status_for("u1", org=35, group=active_group)


def test_status_fields_provider_resolves_org_secret(monkeypatch):
    st = _wire_status(monkeypatch, org_secret=True)()
    zoho = st["providers"]["zoho"]
    assert zoho["mode"] == "org"
    assert zoho["org_secret_configured"] is True


def test_status_fields_provider_resolves_group_secret(monkeypatch):
    st = _wire_status(monkeypatch, group_secret=True, org_secret=True,
                      active_group=2)()
    assert st["providers"]["zoho"]["mode"] == "group"


def test_status_fields_provider_forbidden_carries_team_hint(monkeypatch):
    run = _wire_status(monkeypatch)
    # équipe sales à portée (membre, secret présent, non active)
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [SALES])
    monkeypatch.setattr(access.group_store, "has_group_secret",
                        lambda gid, prov: gid == 2)
    zoho = run()["providers"]["zoho"]
    assert zoho["mode"] == "forbidden"
    assert zoho["team_key_group"] == {"id": 2, "name": "sales"}


def test_status_keyed_provider_forbidden_carries_team_hint(monkeypatch):
    run = _wire_status(monkeypatch)
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [SALES])
    monkeypatch.setattr(access.group_store, "has_group_secret",
                        lambda gid, prov: gid == 2)
    # attio = keyed byo-only (KEY_PROVIDERS) → boucle principale
    attio = run()["providers"]["attio"]
    assert attio["mode"] == "forbidden"
    assert attio["team_key_group"] == {"id": 2, "name": "sales"}
