"""Sélecteur d'identité générique (ADR 0024) : registre par-connecteur.
Google = comptes du coffre ; Unipile = identités distantes d'une clé BYO (validation
id ∈ liste = anti-binding ; BYO-only) ; Pennylane GED = backend enregistré par le
module tools (async, sociétés du cabinet = GED cibles, défaut au meta du credential)."""
import asyncio

import pytest

from oto_mcp import access, browserbase, connector_identities, credentials_store
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
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr("oto_mcp.google_oauth.list_accounts",
                        lambda sub: [{"google_email": "a@x.io", "is_default": True},
                                     {"google_email": "b@x.io", "is_default": False}])
    ids = connector_identities.list_identities("u1", "google")
    assert {i["id"] for i in ids} == {"a@x.io", "b@x.io"}
    assert next(i for i in ids if i["id"] == "a@x.io")["is_default"]

    called = {}
    monkeypatch.setattr("oto_mcp.db.set_default_google_account",
                        lambda sub, org, acc: called.update(acc=acc, org=org) or True)
    connector_identities.select_identity("u1", "google", "b@x.io")
    assert called["acc"] == "b@x.io"


def test_google_select_unknown_raises(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr("oto_mcp.db.set_default_google_account", lambda sub, org, acc: False)
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
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    # #55 : par défaut, pas de grant reçu ni de pointeur « identité opérée ».
    monkeypatch.setattr("oto_mcp.db.list_account_grants_to", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for", lambda sub, prov: {})
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.clear_operated_account", lambda sub, prov: None)


def test_unipile_list_byo(monkeypatch):
    _wire_byo(monkeypatch)
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id", lambda sub, org, ch: "A2")
    ids = connector_identities.list_identities("u1", "unipile")
    assert {i["id"] for i in ids} == {"A1", "A2"}
    a2 = next(i for i in ids if i["id"] == "A2")
    assert a2["is_default"] and a2["channel"] == "LINKEDIN" and a2["label"] == "Laurent Guy"


def test_unipile_select_valid(monkeypatch):
    _wire_byo(monkeypatch)
    saved = {}
    monkeypatch.setattr("oto_mcp.db.set_unipile_account",
                        lambda sub, aid, name, org_id=None, provider="LINKEDIN",
                               platform_seat=False:
                        saved.update(aid=aid, name=name, provider=provider,
                                     org_id=org_id, platform_seat=platform_seat))
    res = connector_identities.select_identity("u1", "unipile", "A1")
    assert res["id"] == "A1" and res["channel"] == "LINKEDIN"
    # Scope membre (ADR 0033 B4) : le binding est rattaché à l'org de contexte,
    # BYO = pas un siège plateforme.
    assert saved == {"aid": "A1", "name": "Alexandra El Hachem",
                     "provider": "LINKEDIN", "org_id": 39, "platform_seat": False}


def test_unipile_select_unknown_id_raises(monkeypatch):
    _wire_byo(monkeypatch)
    monkeypatch.setattr("oto_mcp.db.set_unipile_account",
                        lambda *a, **k: pytest.fail("ne doit pas écrire un id inconnu"))
    with pytest.raises(ValueError, match="inconnu"):
        connector_identities.select_identity("u1", "unipile", "GHOST")


def test_unipile_platform_no_selector(monkeypatch):
    # clé plateforme (revente) SANS grant → liste vide, sélection refusée
    # (hosted-auth conservé, widget strictement inchangé).
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: "platform")
    monkeypatch.setattr("oto_mcp.db.list_account_grants_to", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: [])
    assert connector_identities.list_identities("u1", "unipile") == []
    with pytest.raises(ValueError, match="plateforme"):
        connector_identities.select_identity("u1", "unipile", "A1")


def test_unipile_hosted_lists_own_accounts(monkeypatch):
    # Feedback #132 : revente/hosted SANS grant mais un compte CONNECTÉ → il doit
    # apparaître (le faux [] faisait conclure « aucun compte » à l'agent).
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: "platform")
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr("oto_mcp.db.list_account_grants_to", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for", lambda sub, prov: {})
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts",
                        lambda sub: [{"account_id": "H1", "account_name": "JB Fleury",
                                      "provider": "LINKEDIN", "org_id": 39}])
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id", lambda sub, org, ch: "H1")
    ids = connector_identities.list_identities("u1", "unipile")
    assert [i["id"] for i in ids] == ["H1"]
    assert ids[0]["label"] == "JB Fleury" and ids[0]["channel"] == "LINKEDIN"
    assert ids[0]["is_default"]


def test_unknown_connector_slug_is_an_error():
    # Feedback #162 : slug hors catalogue ≠ « connecteur sans identités ».
    from oto_mcp.capabilities.connectors_identities import _require_known_connector
    from oto_mcp.capabilities._types import AuthzDenied
    _require_known_connector("unipile")  # catalogue → no-op
    with pytest.raises(AuthzDenied) as e:
        _require_known_connector("bidon_zzz")
    assert e.value.code == "unknown_connector" and e.value.status == 404
    with pytest.raises(AuthzDenied, match="unipile"):
        _require_known_connector("linkedin")  # alias fréquent → pointe unipile
