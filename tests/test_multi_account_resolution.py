"""R1a — sélection de compte au palier MEMBRE (multi-compte, « 2 Zoho »).

Compte effectif : account explicite > épinglage projet > compte unique auto >
McpError (jamais de repli muet vers un autre compte / l'org / la plateforme —
anti-usurpation). Gate sur les connecteurs multi-compte : un mono-compte garde la
résolution historique (account='', tenté tel quel). On stubbe les seams DB.
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access


class _MultiCon:
    auth_multi_account = True
    auth_modes = ("byo",)


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(access, "require_connector_access", lambda p, s=None: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.connectors, "connector_for_provider", lambda p: _MultiCon())
    monkeypatch.setattr(access, "project_pinned_identity", lambda prov: None)
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: None)
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)
    yield


def _accounts(monkeypatch, names):
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": n} for n in names])


def _vault(monkeypatch, mapping):
    monkeypatch.setattr(access.db, "get_member_api_key",
                        lambda sub, org, prov, account="": mapping.get(account))


def test_two_accounts_no_pin_raises(monkeypatch):
    _accounts(monkeypatch, ["zoho-fr", "zoho-us"])
    _vault(monkeypatch, {"zoho-fr": "K1", "zoho-us": "K2"})
    with pytest.raises(McpError):
        access.resolve_credential("zoho", sub="u1", emit_on_failure=False)


def test_single_named_account_auto(monkeypatch):
    _accounts(monkeypatch, ["zoho-fr"])
    _vault(monkeypatch, {"zoho-fr": "K1"})
    rc = access.resolve_credential("zoho", sub="u1")
    assert rc.key == "K1" and rc.account == "zoho-fr"


def test_explicit_account_resolves(monkeypatch):
    _accounts(monkeypatch, ["zoho-fr", "zoho-us"])
    _vault(monkeypatch, {"zoho-fr": "K1", "zoho-us": "K2"})
    rc = access.resolve_credential("zoho", sub="u1", account="zoho-us")
    assert rc.key == "K2" and rc.account == "zoho-us"


def test_explicit_account_missing_raises_no_fallthrough(monkeypatch):
    # Un org secret EXISTE, mais un account explicite introuvable ne doit PAS y
    # retomber (agir sous une autre identité que celle demandée = usurpation).
    _vault(monkeypatch, {"zoho-fr": "K1"})
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: "ORGKEY")
    with pytest.raises(McpError):
        access.resolve_credential("zoho", sub="u1", account="zoho-de", emit_on_failure=False)


def test_pinned_identity_selects_account(monkeypatch):
    monkeypatch.setattr(access, "project_pinned_identity", lambda prov: "zoho-us")
    _vault(monkeypatch, {"zoho-fr": "K1", "zoho-us": "K2"})
    rc = access.resolve_credential("zoho", sub="u1")
    assert rc.key == "K2" and rc.account == "zoho-us"


def test_zero_accounts_tries_legacy_empty(monkeypatch):
    # Aucun compte nommé → eff='' → get_member_api_key('') tenté (mono-compte legacy).
    _accounts(monkeypatch, [])
    tried = []
    monkeypatch.setattr(access.db, "get_member_api_key",
                        lambda sub, org, prov, account="": tried.append(account) or None)
    with pytest.raises(McpError):
        access.resolve_credential("zoho", sub="u1", want="byo", emit_on_failure=False)
    assert tried == [""]
