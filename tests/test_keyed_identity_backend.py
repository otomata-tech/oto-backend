"""R1b — backend d'identités keyed générique (multi-compte, ex. « 2 Zoho »).

Tout connecteur de providers.MULTI_ACCOUNT_PROVIDERS sans backend spécifique
(google en a un) expose ses comptes du coffre via oto_connector_identities :
lister = lignes MEMBER de l'org de contexte, select = pose meta.is_default UNIQUE.
"""
import pytest

from oto_mcp import access, connector_identities as ci, credentials_store, providers


def test_zoho_registered_generic():
    assert "zoho" in providers.MULTI_ACCOUNT_PROVIDERS
    assert ci.supports("zoho")
    # google garde son backend SPÉCIFIQUE (le générique ne l'écrase pas).
    assert ci._LISTERS["google"] is ci._google_list


def test_keyed_list_maps_accounts(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(credentials_store, "member_id", lambda org, sub: f"{org}:{sub}")
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [
        {"account": "zoho-fr", "meta": {"label": "Zoho France", "is_default": True}},
        {"account": "zoho-us", "meta": {}},
    ])
    ids = ci.list_identities("u1", "zoho")
    assert ids == [
        {"id": "zoho-fr", "label": "Zoho France", "status": "ok",
         "is_default": True, "channel": None},
        {"id": "zoho-us", "label": "zoho-us", "status": "ok",
         "is_default": False, "channel": None},
    ]


def test_keyed_list_no_org_empty(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    assert ci.list_identities("u1", "zoho") == []


def test_keyed_select_sets_unique_default(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(credentials_store, "member_id", lambda org, sub: "1:u1")
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [
        {"account": "zoho-fr", "meta": {}}, {"account": "zoho-us", "meta": {}}])
    writes = []
    monkeypatch.setattr(
        credentials_store, "update_meta",
        lambda et, eid, con, acct, patch: writes.append((acct, patch["is_default"])))
    res = ci.select_identity("u1", "zoho", "zoho-us")
    assert res["id"] == "zoho-us" and res["is_default"] is True
    assert set(writes) == {("zoho-fr", False), ("zoho-us", True)}


def test_keyed_select_unknown_raises(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(credentials_store, "member_id", lambda org, sub: "1:u1")
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [
        {"account": "zoho-fr", "meta": {}}])
    with pytest.raises(ValueError, match="inconnu"):
        ci.select_identity("u1", "zoho", "zoho-de")
