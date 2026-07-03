"""Secrets multi-champs partageables org/groupe (BYOK) — helper + cascade.

Couvre `credentials_store.secret_from_input` (packing mono/multi-champ) et la
cascade user→groupe→org de `access.resolve_credential_fields`, gatée sur
`org_shareable` (byo_org). Zoho = pilote (4 champs : 3 secrets + data_center).
"""
import pytest

from mcp.shared.exceptions import McpError

from oto_mcp import access, credentials_store


# --- helper secret_from_input -------------------------------------------------

def test_secret_from_input_multi_field_packs_all():
    fields = {"client_id": "a", "client_secret": "b",
              "refresh_token": "c", "data_center": "eu"}
    secret = credentials_store.secret_from_input("zoho", fields=fields)
    assert credentials_store.unpack_secret("zoho", secret) == fields


def test_secret_from_input_multi_field_missing_raises():
    with pytest.raises(ValueError, match="missing_credentials"):
        credentials_store.secret_from_input("zoho", fields={"client_id": "a"})


def test_secret_from_input_single_key_raw():
    assert credentials_store.secret_from_input("serper", api_key="K") == "K"


def test_secret_from_input_single_key_empty_raises():
    with pytest.raises(ValueError, match="empty_api_key"):
        credentials_store.secret_from_input("serper", api_key="  ")


# --- cascade resolve_credential_fields ----------------------------------------

def _pack(**f):
    return credentials_store.pack_secret("zoho", f)


def _wire(monkeypatch, *, user=None, group=None, org=None,
          active_group=7, active_org=42):
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, prov, *a, **k: user)
    monkeypatch.setattr(access, "current_group", lambda sub: active_group)
    monkeypatch.setattr(access, "current_org", lambda sub: active_org)
    monkeypatch.setattr(access.group_store, "get_group_secret",
                        lambda gid, prov: group)
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda oid, prov: org)


def test_user_secret_wins(monkeypatch):
    _wire(monkeypatch, user=_pack(client_id="u", client_secret="b",
                                  refresh_token="c", data_center="com"),
          group=_pack(client_id="g", client_secret="b",
                      refresh_token="c", data_center="eu"))
    assert access.resolve_credential_fields("zoho")["client_id"] == "u"


def test_group_secret_when_no_user(monkeypatch):
    _wire(monkeypatch, user=None,
          group=_pack(client_id="g", client_secret="b",
                      refresh_token="c", data_center="eu"),
          org=_pack(client_id="o", client_secret="b",
                    refresh_token="c", data_center="eu"))
    creds = access.resolve_credential_fields("zoho")
    assert creds["client_id"] == "g" and creds["data_center"] == "eu"


def test_org_secret_when_no_user_no_group(monkeypatch):
    _wire(monkeypatch, user=None, group=None,
          org=_pack(client_id="o", client_secret="b",
                    refresh_token="c", data_center="eu"))
    assert access.resolve_credential_fields("zoho")["client_id"] == "o"


def test_nothing_set_raises(monkeypatch):
    _wire(monkeypatch, user=None, group=None, org=None)
    with pytest.raises(McpError):
        access.resolve_credential_fields("zoho")


def test_non_shareable_ignores_group_org(monkeypatch):
    # silae = byo_user only (org_shareable False) → groupe/org jamais consultés.
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda *a, **k: None)
    monkeypatch.setattr(access, "current_group", lambda sub: 7)
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    called = {"group": False, "org": False}
    monkeypatch.setattr(access.group_store, "get_group_secret",
                        lambda *a: called.__setitem__("group", True) or "x")
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda *a: called.__setitem__("org", True) or "x")
    with pytest.raises(McpError):
        access.resolve_credential_fields("silae")
    assert called == {"group": False, "org": False}


def test_secret_from_input_optional_fields_subset():
    # slack : bot_token/user_token facultatifs (« ET/OU ») — un seul suffit,
    # packé JSON (le schéma déclare 2 champs), zéro au total refusé.
    packed = credentials_store.secret_from_input(
        "slack", fields={"user_token": "xoxp-abc"})
    assert credentials_store.unpack_secret("slack", packed) == {"user_token": "xoxp-abc"}
    with pytest.raises(ValueError, match="missing_credentials"):
        credentials_store.secret_from_input("slack", fields={})
