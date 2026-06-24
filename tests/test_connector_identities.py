"""Sélecteur d'identité générique (ADR 0024) : registre par-connecteur.
Google = comptes du coffre ; Unipile = identités distantes d'une clé BYO (validation
id ∈ liste = anti-binding ; BYO-only)."""
import pytest

from oto_mcp import access, connector_identities
from oto_mcp.access import ResolvedCredential


class _FakeUnipile:
    ACCOUNTS = [
        {"id": "A1", "name": "Alexandra El Hachem", "type": "LINKEDIN",
         "sources": [{"status": "OK"}]},
        {"id": "A2", "name": "Laurent Guy", "type": "LINKEDIN", "sources": [{"status": "OK"}]},
    ]

    def __init__(self, api_key=None, dsn=None, **k):
        self.api_key, self.dsn = api_key, dsn

    def list_accounts(self):
        return self.ACCOUNTS


# --- Google -----------------------------------------------------------------

def test_google_list_and_select(monkeypatch):
    monkeypatch.setattr("oto_mcp.google_oauth.list_accounts",
                        lambda sub: [{"google_email": "a@x.io", "is_default": True},
                                     {"google_email": "b@x.io", "is_default": False}])
    ids = connector_identities.list_identities("u1", "google")
    assert {i["id"] for i in ids} == {"a@x.io", "b@x.io"}
    assert next(i for i in ids if i["id"] == "a@x.io")["is_default"]

    called = {}
    monkeypatch.setattr("oto_mcp.db.set_default_google_account",
                        lambda sub, acc: called.update(acc=acc) or True)
    connector_identities.select_identity("u1", "google", "b@x.io")
    assert called["acc"] == "b@x.io"


def test_google_select_unknown_raises(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.set_default_google_account", lambda sub, acc: False)
    with pytest.raises(ValueError):
        connector_identities.select_identity("u1", "google", "ghost@x.io")


# --- Unipile (BYO) ----------------------------------------------------------

def _wire_byo(monkeypatch, mode="org"):
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: mode)
    monkeypatch.setattr(access, "resolve_credential",
                        lambda prov, want="auto", sub=None: ResolvedCredential(
                            "unipile", "KEY", False, "org", "org", "39"))
    # config.dsn lu par _unipile_client
    monkeypatch.setattr(access.credentials_store, "get_credential_with_meta",
                        lambda *a, **k: {"meta": {"dsn": "api6.unipile.com:13616"}})
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeUnipile)


def test_unipile_list_byo(monkeypatch):
    _wire_byo(monkeypatch)
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id", lambda sub, ch: "A2")
    ids = connector_identities.list_identities("u1", "unipile")
    assert {i["id"] for i in ids} == {"A1", "A2"}
    a2 = next(i for i in ids if i["id"] == "A2")
    assert a2["is_default"] and a2["channel"] == "LINKEDIN" and a2["label"] == "Laurent Guy"


def test_unipile_select_valid(monkeypatch):
    _wire_byo(monkeypatch)
    saved = {}
    monkeypatch.setattr("oto_mcp.db.set_unipile_account",
                        lambda sub, aid, name, org_id=None, provider="LINKEDIN":
                        saved.update(aid=aid, name=name, provider=provider, org_id=org_id))
    res = connector_identities.select_identity("u1", "unipile", "A1")
    assert res["id"] == "A1" and res["channel"] == "LINKEDIN"
    assert saved == {"aid": "A1", "name": "Alexandra El Hachem",
                     "provider": "LINKEDIN", "org_id": None}


def test_unipile_select_unknown_id_raises(monkeypatch):
    _wire_byo(monkeypatch)
    monkeypatch.setattr("oto_mcp.db.set_unipile_account",
                        lambda *a, **k: pytest.fail("ne doit pas écrire un id inconnu"))
    with pytest.raises(ValueError, match="inconnu"):
        connector_identities.select_identity("u1", "unipile", "GHOST")


def test_unipile_platform_no_selector(monkeypatch):
    # clé plateforme (revente) → liste vide, sélection refusée (hosted-auth conservé).
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: "platform")
    assert connector_identities.list_identities("u1", "unipile") == []
    with pytest.raises(ValueError, match="plateforme"):
        connector_identities.select_identity("u1", "unipile", "A1")
