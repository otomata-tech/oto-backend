"""Phase 2 (2026-08-25) — comptes nommés aux paliers PARTAGÉS (org / équipe).

Même sélection qu'au palier membre (`_pick_account`) : `_account=` explicite >
épinglage projet > compte unique auto > défaut posé > McpError. La ligne legacy
`''` d'une org reste résolue telle quelle. Seams DB stubbés.
"""
import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import access, credentials_store
from oto_mcp.connectors import identities as connector_identities


class _MultiCon:
    auth_multi_account = True
    auth_modes = ("byo", "byo_org")
    name = "serper"


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.providers, "connector_for_provider", lambda p: _MultiCon())
    monkeypatch.setattr(access.providers, "is_byo_user", lambda p: True)
    monkeypatch.setattr(access, "project_pinned_identity", lambda prov: None)
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", {"serper"})
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)
    # Aucune clé membre : la cascade descend au palier org.
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    yield


def _org_accounts(monkeypatch, names, default=None):
    rows = [{"account": n, "meta": {"is_default": n == default}} for n in names]
    def _list(et, eid, con):
        return rows if et == "org" else []
    monkeypatch.setattr(access.credentials_store, "list_accounts", _list)


def _org_vault(monkeypatch, mapping):
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda oid, prov, account="": mapping.get(account))


def test_org_legacy_single_row_resolves_as_before(monkeypatch):
    _org_accounts(monkeypatch, [""])
    _org_vault(monkeypatch, {"": "K-ORG"})
    rc = access.resolve_credential("serper", sub="u1")
    assert rc.key == "K-ORG" and rc.mode == "org" and rc.account == ""


def test_org_single_named_account_auto(monkeypatch):
    _org_accounts(monkeypatch, ["eu"])
    _org_vault(monkeypatch, {"eu": "K-EU"})
    rc = access.resolve_credential("serper", sub="u1")
    assert rc.key == "K-EU" and rc.account == "eu" and rc.entity_type == "org"


def test_org_two_accounts_need_a_choice(monkeypatch):
    _org_accounts(monkeypatch, ["eu", "us"])
    _org_vault(monkeypatch, {"eu": "K-EU", "us": "K-US"})
    with pytest.raises(McpError):
        access.resolve_credential("serper", sub="u1", emit_on_failure=False)


def test_org_default_account_wins(monkeypatch):
    _org_accounts(monkeypatch, ["eu", "us"], default="us")
    _org_vault(monkeypatch, {"eu": "K-EU", "us": "K-US"})
    rc = access.resolve_credential("serper", sub="u1")
    assert rc.key == "K-US" and rc.account == "us"


def test_org_explicit_account_param(monkeypatch):
    _org_accounts(monkeypatch, ["eu", "us"])
    _org_vault(monkeypatch, {"eu": "K-EU", "us": "K-US"})
    rc = access.resolve_credential("serper", sub="u1", account="eu")
    assert rc.key == "K-EU" and rc.account == "eu"


def test_org_unknown_explicit_account_raises_not_falls_back(monkeypatch):
    _org_accounts(monkeypatch, ["eu"])
    _org_vault(monkeypatch, {"eu": "K-EU"})
    with pytest.raises(McpError):
        access.resolve_credential("serper", sub="u1", account="nope", emit_on_failure=False)


# --- identités : scope org/group sur le backend keyed générique ----------------

def test_identities_list_scoped_to_org(monkeypatch):
    seen = {}
    def _list(et, eid, con):
        seen["ent"] = (et, eid)
        return [{"account": "eu", "meta": {"is_default": True}, "set_at": None}]
    monkeypatch.setattr(credentials_store, "list_accounts", _list)
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    ids = connector_identities.list_identities("u1", "serper", scope="org")
    assert seen["ent"] == ("org", "7")
    assert ids == [{"id": "eu", "label": "eu", "status": "ok", "is_default": True, "channel": None}]


def test_identities_select_scoped_to_group(monkeypatch):
    calls = []
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": "a", "meta": {}}, {"account": "b", "meta": {}}])
    import contextlib
    from oto_mcp.db import _conn, regles_de_comptes
    monkeypatch.setattr(_conn, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(regles_de_comptes, "poser_le_defaut",
                        lambda conn, et, eid, con, acct, *, par:
                        calls.append((et, eid, acct, par)))
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "current_group", lambda sub: 42)
    res = connector_identities.select_identity("u1", "serper", "b", scope="group")
    assert res["id"] == "b" and res["is_default"] is True
    assert calls == [("group", "42", "b", "u1")]


def test_identities_bespoke_backend_is_member_only():
    assert connector_identities.list_identities("u1", "google", scope="org") == []
    with pytest.raises(ValueError):
        connector_identities.select_identity("u1", "google", "x", scope="org")


# --- coexistence '' / nommés, partagée par les paliers ----------------------------

def test_named_coexistence_migrates_legacy_row(monkeypatch):
    renamed = []
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": ""}, {"account": "principal"}])
    monkeypatch.setattr(credentials_store, "rename_account",
                        lambda et, eid, con, old, new: renamed.append((old, new)) or True)
    credentials_store.ensure_named_coexistence("org", "7", "serper", "eu")
    assert renamed == [("", "principal-2")]


def test_named_coexistence_refuses_anonymous_next_to_named(monkeypatch):
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [{"account": "eu"}])
    with pytest.raises(credentials_store.NamedAccountRequired):
        credentials_store.ensure_named_coexistence("org", "7", "serper", "")


def test_le_refus_de_pose_anonyme_nomme_les_comptes_et_le_geste(monkeypatch):
    """Vécu 30/09 (Slack, deux workspaces) : le refus remonte tel quel au dashboard.
    « précise `account` » ne parle qu'à un agent — il doit nommer les comptes et le
    geste qui les remplace."""
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda *a, **k: [{"account": "principal"}, {"account": "otomata admin"}])
    with pytest.raises(credentials_store.NamedAccountRequired) as e:
        credentials_store.ensure_named_coexistence("member", "2:u", "slack", "")
    msg = str(e.value)
    assert "« otomata admin »" in msg and "« principal »" in msg
    assert "Remplacer" in msg
