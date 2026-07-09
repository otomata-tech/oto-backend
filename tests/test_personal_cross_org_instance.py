"""Issue #172, piste A — instance PERSONNELLE cross-org (amende ADR 0033).

Un connecteur intrinsèquement PAR-PERSONNE (`Connector.personal_cross_org`, ex.
unipile : le compte de messagerie hébergé EST l'humain) voit sa clé membre suivre
le `sub` dans TOUTES ses orgs — résolution de proximité, pas seulement pin
`instance=`. Invariants couverts :

1. le flag registre est bien posé sur unipile (et seulement lui aujourd'hui) ;
2. `access.personal_instance_org` est déterministe (perso > plus récente, exclut
   l'org de contexte) ;
3. `resolve_credential` retombe sur l'instance perso d'une AUTRE org QUAND la clé
   membre locale manque — et SEULEMENT pour un connecteur `personal_cross_org` ;
4. `credential_mode_for` MIROITE la résolution (sinon l'UI mentirait « platform ») ;
5. l'account_id Unipile suit la MÊME org que la clé (clé et compte appariés).
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access, connectors, connector_identities, credentials_store


# --- 1. flag registre -----------------------------------------------------------

def test_unipile_is_personal_cross_org():
    assert connectors.is_personal_cross_org("unipile") is True
    assert "unipile" in connectors.PERSONAL_CROSS_ORG_PROVIDERS


def test_ordinary_connector_is_not_personal_cross_org():
    # Un connecteur à clé partagée classique reste strictement (sub, org) — ADR 0033.
    assert connectors.is_personal_cross_org("pennylane") is False
    assert connectors.is_personal_cross_org("google") is False


# --- 2. choix déterministe de l'org porteuse ------------------------------------

def test_personal_instance_org_prefers_personal_org(monkeypatch):
    monkeypatch.setattr(credentials_store, "list_member_orgs_for",
                        lambda sub, con: [5, 2, 9])  # set_at DESC
    monkeypatch.setattr(access.org_store, "get_personal_org", lambda sub: 2)
    assert access.personal_instance_org("u1", "unipile") == 2


def test_personal_instance_org_falls_back_to_most_recent(monkeypatch):
    # Pas d'org perso parmi les porteuses → la plus récente (tête de liste DESC).
    monkeypatch.setattr(credentials_store, "list_member_orgs_for",
                        lambda sub, con: [5, 2, 9])
    monkeypatch.setattr(access.org_store, "get_personal_org", lambda sub: 999)
    assert access.personal_instance_org("u1", "unipile") == 5


def test_personal_instance_org_excludes_context_org(monkeypatch):
    # L'org de contexte (déjà testée par le palier membre local) est écartée.
    monkeypatch.setattr(credentials_store, "list_member_orgs_for",
                        lambda sub, con: [2])
    monkeypatch.setattr(access.org_store, "get_personal_org", lambda sub: None)
    assert access.personal_instance_org("u1", "unipile", exclude_org=2) is None


def test_personal_instance_org_none_when_no_member_key(monkeypatch):
    monkeypatch.setattr(credentials_store, "list_member_orgs_for", lambda sub, con: [])
    monkeypatch.setattr(access.org_store, "get_personal_org", lambda sub: 2)
    assert access.personal_instance_org("u1", "unipile") is None


# --- 3. résolution de proximité cross-org ---------------------------------------

def _wire_resolution(monkeypatch, *, current, key_orgs, personal=None):
    """current = org de contexte ; key_orgs = orgs portant une clé membre unipile."""
    monkeypatch.setattr(access, "require_connector_access", lambda p, s=None: None)
    monkeypatch.setattr(access, "current_org", lambda sub: current)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(
        access.db, "get_member_api_key",
        lambda sub, org, prov, account="": f"K-{org}" if org in key_orgs else None)
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: None)
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)
    monkeypatch.setattr(credentials_store, "list_member_orgs_for",
                        lambda sub, con: sorted(key_orgs, reverse=True))
    monkeypatch.setattr(access.org_store, "get_personal_org", lambda sub: personal)


def test_resolve_uses_personal_instance_from_other_org(monkeypatch):
    # Contexte org 168 SANS clé locale, clé perso dans l'org 2 → résout la clé d'org 2
    # (mode 'user', entity_id = '2:sub') SANS reconnexion. C'est la repro de #172.
    _wire_resolution(monkeypatch, current=168, key_orgs={2})
    rc = access.resolve_credential("unipile", sub="u1")
    assert rc.key == "K-2" and rc.mode == "user"
    assert rc.entity_type == "member" and rc.entity_id == "2:u1"


def test_local_member_key_still_wins(monkeypatch):
    # Clé locale présente → chemin historique inchangé (pas de détour cross-org).
    _wire_resolution(monkeypatch, current=2, key_orgs={2, 5})
    rc = access.resolve_credential("unipile", sub="u1")
    assert rc.key == "K-2" and rc.entity_id == "2:u1"


def test_no_cross_org_fallback_for_ordinary_connector(monkeypatch):
    # pennylane n'est PAS personal_cross_org → pas de clé locale = McpError (byo-only),
    # jamais la clé d'une autre org (ADR 0033 préservé).
    _wire_resolution(monkeypatch, current=168, key_orgs={2})
    with pytest.raises(McpError):
        access.resolve_credential("pennylane", sub="u1", emit_on_failure=False)


# --- 4. credential_mode_for miroite la résolution -------------------------------

def test_mode_for_reports_user_via_cross_org_instance(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 168)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.db, "has_member_api_key",
                        lambda sub, org, prov: org == 2)  # locale absente, perso en 2
    monkeypatch.setattr(credentials_store, "list_member_orgs_for", lambda sub, con: [2])
    monkeypatch.setattr(access.org_store, "get_personal_org", lambda sub: 2)
    assert access.credential_mode_for("u1", "unipile") == "user"


# --- 5. account_id Unipile suit l'org de la clé ---------------------------------

def test_own_account_id_falls_back_cross_org(monkeypatch):
    # Pas de compte dans l'org de contexte (168), mais un compte dans l'org perso (2)
    # → on renvoie CELUI de l'org 2 (apparié à la clé qui résout, #172).
    monkeypatch.setattr(access, "current_org", lambda sub: 168)
    monkeypatch.setattr(access, "personal_instance_org",
                        lambda sub, con, exclude_org=None: 2)
    # is_personal_cross_org("unipile") = réel (import lazy dans le helper).
    monkeypatch.setattr(
        "oto_mcp.db.get_unipile_account_id",
        lambda sub, org, prov: "ACC-2" if org == 2 else None)
    monkeypatch.setattr("oto_mcp.db.get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for", lambda sub, prov: {})
    assert connector_identities.resolve_operated_account_id("u1", "LINKEDIN") == "ACC-2"


def test_own_account_id_prefers_context_org(monkeypatch):
    # Compte présent dans l'org de contexte → pas de détour cross-org.
    monkeypatch.setattr(access, "current_org", lambda sub: 168)
    called = {"cross": False}

    def _pio(sub, con, exclude_org=None):
        called["cross"] = True
        return 2
    monkeypatch.setattr(access, "personal_instance_org", _pio)
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id",
                        lambda sub, org, prov: "ACC-168" if org == 168 else None)
    monkeypatch.setattr("oto_mcp.db.get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for", lambda sub, prov: {})
    assert connector_identities.resolve_operated_account_id("u1", "LINKEDIN") == "ACC-168"
    assert called["cross"] is False
