"""B3 : unipile_client tire le DSN de la config du credential résolu (apparié à
la clé gagnante de la cascade). Clé plateforme → DSN None (instance env/défaut).
+ #55 : le grant de compte partagé = SEULE exception au no-fallback anti-usurpation
(pointeur « identité opérée » revalidé à chaque appel, pin projet accepté si accordé)."""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access
from oto_mcp.access import ResolvedCredential
from oto_mcp.tools import unipile as unipile_tool


class _FakeClient:
    def __init__(self, api_key=None, account_id=None, dsn=None):
        self.api_key, self.account_id, self.dsn = api_key, account_id, dsn


def _no_grants(monkeypatch):
    """Défauts #55 : pas de pointeur opéré, pas de grant reçu."""
    monkeypatch.setattr(unipile_tool.db, "get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr(unipile_tool.db, "granted_accounts_for", lambda sub, prov: {})


def test_unipile_client_byo_passes_dsn(monkeypatch):
    rc = ResolvedCredential("unipile", "KEY", False, "org", "org", "39")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(
        access.credentials_store, "get_credential_with_meta",
        lambda et, eid, prov, account="": {
            "meta": {"dsn": "api6.unipile.com:13616"}, "secret": "KEY", "set_at": None})
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(unipile_tool.db, "get_unipile_account_id", lambda sub, prov: "ACC")
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    _no_grants(monkeypatch)

    cli = unipile_tool.unipile_client("LINKEDIN")
    assert cli.dsn == "api6.unipile.com:13616"
    assert cli.api_key == "KEY" and cli.account_id == "ACC"


def test_unipile_client_platform_dsn_none(monkeypatch):
    rc = ResolvedCredential("unipile", "PK", True, "platform")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(unipile_tool.db, "get_unipile_account_id", lambda sub, prov: "ACC")
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    _no_grants(monkeypatch)

    cli = unipile_tool.unipile_client("LINKEDIN")
    assert cli.dsn is None and cli.api_key == "PK"


def _wire_basic(monkeypatch):
    rc = ResolvedCredential("unipile", "PK", True, "platform")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(unipile_tool.db, "get_unipile_account_id", lambda sub, prov: "DEFAULT")
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    _no_grants(monkeypatch)


def test_project_pin_applied_when_owned_and_channel_match(monkeypatch):
    # #57 : le pin projet prime SI le compte appartient au sub ET au canal demandé.
    _wire_basic(monkeypatch)
    monkeypatch.setattr(access, "project_pinned_identity", lambda c: "PINNED")
    monkeypatch.setattr(unipile_tool.db, "list_unipile_accounts",
                        lambda sub: [{"account_id": "PINNED", "provider": "LINKEDIN"}])
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "PINNED"


def test_project_pin_rejected_when_not_owned(monkeypatch):
    # Anti-usurpation : un compte épinglé qui n'est PAS au sub est ignoré (repli défaut).
    _wire_basic(monkeypatch)
    monkeypatch.setattr(access, "project_pinned_identity", lambda c: "OTHER_USER_ACC")
    monkeypatch.setattr(unipile_tool.db, "list_unipile_accounts",
                        lambda sub: [{"account_id": "DEFAULT", "provider": "LINKEDIN"}])
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "DEFAULT"


def test_project_pin_rejected_on_channel_mismatch(monkeypatch):
    # Le pin LinkedIn ne s'applique pas à un outil WhatsApp (repli défaut).
    _wire_basic(monkeypatch)
    monkeypatch.setattr(access, "project_pinned_identity", lambda c: "PINNED")
    monkeypatch.setattr(unipile_tool.db, "list_unipile_accounts",
                        lambda sub: [{"account_id": "PINNED", "provider": "LINKEDIN"}])
    assert unipile_tool.unipile_client("WHATSAPP").account_id == "DEFAULT"


# --- #55 : compte accordé (pointeur « identité opérée » + pin projet) --------

def test_operated_account_resolves_when_granted(monkeypatch):
    # Le pointeur opéré posé + grant vivant → on agit comme le compte du owner.
    _wire_basic(monkeypatch)
    monkeypatch.setattr(unipile_tool.db, "get_operated_account",
                        lambda sub, prov: {"account_id": "OWNER_ACC", "owner_sub": "owner"})
    monkeypatch.setattr(unipile_tool.db, "granted_accounts_for",
                        lambda sub, prov: {"OWNER_ACC": {"owner_sub": "owner",
                                                         "owner_email": "o@x.io"}})
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "OWNER_ACC"


def test_operated_account_revoked_raises_no_silent_fallback(monkeypatch):
    # Grant révoqué (ou owner déconnecté) MAIS pointeur encore posé → erreur
    # EXPLICITE, jamais de repli silencieux sur le compte propre (identité ≠ donnée).
    _wire_basic(monkeypatch)
    monkeypatch.setattr(unipile_tool.db, "get_operated_account",
                        lambda sub, prov: {"account_id": "OWNER_ACC", "owner_sub": "owner"})
    monkeypatch.setattr(unipile_tool.db, "granted_accounts_for", lambda sub, prov: {})
    with pytest.raises(McpError, match="plus opérable"):
        unipile_tool.unipile_client("LINKEDIN")


def test_operated_account_channel_scoped(monkeypatch):
    # Le pointeur LinkedIn n'affecte pas WhatsApp (pointeur ET grant par canal).
    _wire_basic(monkeypatch)
    monkeypatch.setattr(
        unipile_tool.db, "get_operated_account",
        lambda sub, prov: ({"account_id": "OWNER_ACC", "owner_sub": "owner"}
                           if prov == "LINKEDIN" else None))
    monkeypatch.setattr(
        unipile_tool.db, "granted_accounts_for",
        lambda sub, prov: ({"OWNER_ACC": {"owner_sub": "owner", "owner_email": None}}
                           if prov == "LINKEDIN" else {}))
    assert unipile_tool.unipile_client("WHATSAPP").account_id == "DEFAULT"


def test_project_pin_accepted_when_granted(monkeypatch):
    # #55 rouvre le pin projet Unipile : un pin sur un compte ACCORDÉ (pas possédé)
    # est honoré — la seule exception au no-fallback, re-checkée à cet appel.
    _wire_basic(monkeypatch)
    monkeypatch.setattr(access, "project_pinned_identity", lambda c: "OWNER_ACC")
    monkeypatch.setattr(unipile_tool.db, "list_unipile_accounts",
                        lambda sub: [{"account_id": "DEFAULT", "provider": "LINKEDIN"}])
    monkeypatch.setattr(unipile_tool.db, "granted_accounts_for",
                        lambda sub, prov: {"OWNER_ACC": {"owner_sub": "owner",
                                                         "owner_email": None}})
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "OWNER_ACC"


def test_project_pin_inert_after_revoke(monkeypatch):
    # Pin sur un compte accordé puis révoqué → pin inerte (fail-soft #57 : repli défaut).
    _wire_basic(monkeypatch)
    monkeypatch.setattr(access, "project_pinned_identity", lambda c: "OWNER_ACC")
    monkeypatch.setattr(unipile_tool.db, "list_unipile_accounts",
                        lambda sub: [{"account_id": "DEFAULT", "provider": "LINKEDIN"}])
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "DEFAULT"
