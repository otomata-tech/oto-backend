"""#55 — sélecteur d'identité avec comptes ACCORDÉS : la liste inclut les comptes
partagés (label owner), le select d'un compte accordé pose le POINTEUR sans toucher
la ligne de connexion du grantee, le retour-à-soi efface le pointeur."""
import pytest

from oto_mcp import access, connector_identities


_GRANT = {
    "provider": "LINKEDIN", "owner_sub": "owner", "owner_email": "anna@x.io",
    "owner_name": "Anna", "account_id": "OWNER_ACC", "account_name": "Anna K",
    "granted_at": "2026-07-01 10:00:00", "active": True,
}


def _wire_platform(monkeypatch, *, grants, own=None):
    """Mode revente (clé plateforme, pas de sélecteur BYO) + grants reçus stubés."""
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: "platform")
    monkeypatch.setattr(access, "current_org", lambda sub: 3)
    monkeypatch.setattr("oto_mcp.db.list_account_grants_to", lambda sub: grants)
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: own or [])
    monkeypatch.setattr("oto_mcp.db.get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for", lambda sub, prov: {})
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id", lambda sub, org, prov: None)


def test_list_platform_mode_includes_granted_with_owner_label(monkeypatch):
    _wire_platform(monkeypatch, grants=[_GRANT],
                   own=[{"provider": "LINKEDIN", "account_id": "MY_ACC",
                         "account_name": "Moi", "org_id": 3, "connected_at": "x"}])
    ids = connector_identities.list_identities("grantee", "unipile")
    by_id = {i["id"]: i for i in ids}
    # Comptes propres listés (retour-à-soi possible) + compte accordé annoté.
    assert set(by_id) == {"MY_ACC", "OWNER_ACC"}
    g = by_id["OWNER_ACC"]
    assert g["granted"] is True and g["owner"]["sub"] == "owner"
    assert "Anna" in g["label"] and g["channel"] == "LINKEDIN"


def test_list_platform_mode_empty_without_grants(monkeypatch):
    # Revente sans grant : liste vide, strictement comme avant #55.
    _wire_platform(monkeypatch, grants=[],
                   own=[{"provider": "LINKEDIN", "account_id": "MY_ACC",
                         "account_name": "Moi", "org_id": 3, "connected_at": "x"}])
    assert connector_identities.list_identities("grantee", "unipile") == []


def test_list_skips_inactive_grants(monkeypatch):
    # Owner déconnecté → grant inerte, absent de la liste.
    _wire_platform(monkeypatch, grants=[{**_GRANT, "active": False, "account_id": None}])
    assert connector_identities.list_identities("grantee", "unipile") == []


def test_select_granted_sets_pointer_not_unipile_accounts(monkeypatch):
    _wire_platform(monkeypatch, grants=[_GRANT])
    monkeypatch.setattr("oto_mcp.db.set_unipile_account",
                        lambda *a, **k: pytest.fail(
                            "select d'un compte ACCORDÉ ne doit JAMAIS écraser la "
                            "ligne de connexion du grantee"))
    pointer = {}
    monkeypatch.setattr("oto_mcp.db.set_operated_account",
                        lambda sub, prov, aid, owner: pointer.update(
                            sub=sub, prov=prov, aid=aid, owner=owner))
    res = connector_identities.select_identity("grantee", "unipile", "OWNER_ACC")
    assert res["granted"] is True and res["channel"] == "LINKEDIN"
    assert pointer == {"sub": "grantee", "prov": "LINKEDIN",
                       "aid": "OWNER_ACC", "owner": "owner"}


def test_select_own_account_clears_pointer(monkeypatch):
    _wire_platform(monkeypatch, grants=[_GRANT],
                   own=[{"provider": "LINKEDIN", "account_id": "MY_ACC",
                         "account_name": "Moi", "org_id": 3, "connected_at": "x"}])
    cleared = {}
    monkeypatch.setattr("oto_mcp.db.clear_operated_account",
                        lambda sub, prov: cleared.update(sub=sub, prov=prov))
    res = connector_identities.select_identity("grantee", "unipile", "MY_ACC")
    assert res["is_default"] and "granted" not in res
    assert cleared == {"sub": "grantee", "prov": "LINKEDIN"}


def test_select_unknown_id_rejected(monkeypatch):
    # Ni accordé, ni propre, ni sur une clé BYO (revente) → refus net (anti-binding).
    _wire_platform(monkeypatch, grants=[_GRANT])
    with pytest.raises(ValueError):
        connector_identities.select_identity("grantee", "unipile", "GHOST")


def test_resolver_pointer_valid_returns_granted_account(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.get_operated_account",
                        lambda sub, prov: {"account_id": "OWNER_ACC", "owner_sub": "owner"})
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for",
                        lambda sub, prov: {"OWNER_ACC": {"owner_sub": "owner",
                                                         "owner_email": None}})
    assert connector_identities.resolve_operated_account_id("grantee", "LINKEDIN") == "OWNER_ACC"


def test_resolver_pointer_revoked_raises(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.get_operated_account",
                        lambda sub, prov: {"account_id": "OWNER_ACC", "owner_sub": "owner"})
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for", lambda sub, prov: {})
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id",
                        lambda sub, org, prov: pytest.fail("pas de repli silencieux"))
    with pytest.raises(ValueError, match="plus opérable"):
        connector_identities.resolve_operated_account_id("grantee", "LINKEDIN")


def test_resolver_no_pointer_returns_own(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 3)
    monkeypatch.setattr("oto_mcp.db.get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id", lambda sub, org, prov: "MY_ACC")
    assert connector_identities.resolve_operated_account_id("grantee", "LINKEDIN") == "MY_ACC"
