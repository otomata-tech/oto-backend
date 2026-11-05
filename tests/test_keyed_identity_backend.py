"""R1b — backend d'identités keyed générique (multi-compte, ex. « 2 Zoho »).

Tout connecteur multi-compte sans backend spécifique
(google en a un) expose ses comptes du coffre via oto_connector_identities :
lister = lignes MEMBER de l'org de contexte, select = pose meta.is_default UNIQUE.
"""
import pytest

from oto_mcp import access, credentials_store, providers
from oto_mcp.connectors import identities as ci


def test_zoho_registered_generic():
    assert providers.REGISTRY["zoho"].auth_multi_account
    assert ci.supports("zoho")
    # google garde son backend SPÉCIFIQUE (le générique ne l'écrase pas).
    assert ci._LISTERS["google"] is ci._google_list


def test_folk_registered_generic():
    # folk : N clés API personnelles nommées (ex. plusieurs workspaces Folk),
    # même backend générique que zoho — aucun code par-connecteur requis.
    assert providers.REGISTRY["folk"].auth_multi_account
    assert ci.supports("folk")
    assert ci._LISTERS["folk"] is not ci._google_list


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
    import contextlib
    from oto_mcp.db import _conn, regles_de_comptes
    writes = []
    monkeypatch.setattr(_conn, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(regles_de_comptes, "poser_le_defaut",
                        lambda conn, et, eid, con, acct, *, par:
                        writes.append((conn, et, eid, con, acct, par)))
    res = ci.select_identity("u1", "zoho", "zoho-us")
    assert res["id"] == "zoho-us" and res["is_default"] is True
    # Défaut UNIQUE du palier, posé avec son auteur (D3, #1499) — en une écriture.
    assert writes == [("conn", "member", "1:u1", "zoho", "zoho-us", "u1")]


def test_keyed_select_unknown_raises(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(credentials_store, "member_id", lambda org, sub: "1:u1")
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [
        {"account": "zoho-fr", "meta": {}}])
    with pytest.raises(ValueError, match="Unknown account"):
        ci.select_identity("u1", "zoho", "zoho-de")
