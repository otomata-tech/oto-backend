"""Substrat connecteurs (Partie 1, B0) : dérivation config (champs non-secrets)
et split secret/config. Pur — pas d'accès coffre.

`Connector.config_fields` = champs `secret=False` (endpoint/host : base_url n8n/make,
data_center zoho…). `credentials_store.split_secret_config` sépare un dict unpacké.
"""
import pytest

from mcp.shared.exceptions import McpError

from oto_mcp import access, credentials_store, providers


def test_config_fields_zoho_has_data_center_only():
    con = providers.REGISTRY["zoho"]
    names = {f.name for f in con.config_fields}
    # data_center est non-secret ; les 3 secrets OAuth n'y sont pas.
    assert "data_center" in names
    assert "client_secret" not in names
    assert "refresh_token" not in names


def test_config_fields_base_url_for_n8n_make():
    for prov in ("n8n", "make"):
        names = {f.name for f in providers.REGISTRY[prov].config_fields}
        assert "base_url" in names


def test_config_fields_empty_for_pure_api_key():
    # serper = api_key simple → champ "key" secret → aucune config.
    assert providers.REGISTRY["serper"].config_fields == ()


def test_split_secret_config_zoho():
    fields = {"client_id": "a", "client_secret": "b",
              "refresh_token": "c", "data_center": "eu"}
    secrets, config = credentials_store.split_secret_config("zoho", fields)
    assert config == {"data_center": "eu"}
    assert secrets == {"client_id": "a", "client_secret": "b", "refresh_token": "c"}


def test_split_secret_config_unknown_field_treated_secret():
    # Champ hors schéma → prudemment secret (jamais exposé comme config).
    secrets, config = credentials_store.split_secret_config("serper", {"key": "K", "x": "y"})
    assert config == {}
    assert secrets == {"key": "K", "x": "y"}


# --- B1 : resolve_credential (cascade + config appariée à la clé gagnante) -----

def _wire(monkeypatch, *, user=None, group=None, org=None, meta=None,
          active_group=7, active_org=42):
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, prov: user)
    monkeypatch.setattr(access, "current_group", lambda sub: active_group)
    monkeypatch.setattr(access, "current_org", lambda sub: active_org)
    monkeypatch.setattr(access.group_store, "get_group_secret", lambda gid, prov: group)
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda oid, prov: org)
    monkeypatch.setattr(
        access.credentials_store, "get_credential_with_meta",
        lambda et, eid, prov, account="": {"meta": meta or {}, "secret": "x", "set_at": None})


def test_resolve_credential_config_from_declared_field(monkeypatch):
    # zoho org-level : data_center (non-secret déclaré) ressort en config.
    sec = credentials_store.pack_secret("zoho", {
        "client_id": "o", "client_secret": "b", "refresh_token": "c", "data_center": "eu"})
    _wire(monkeypatch, org=sec, meta={})
    rc = access.resolve_credential("zoho")
    assert rc.mode == "org" and rc.is_platform is False
    assert rc.config.get("data_center") == "eu"
    assert "client_secret" not in rc.config  # secret jamais en config


def test_resolve_credential_config_from_meta_dsn(monkeypatch):
    # unipile keyed : le dsn vit dans meta → ressort en config, apparié à la clé.
    _wire(monkeypatch, org="UNIPILE_KEY", meta={"dsn": "api6.unipile.com:13616"})
    rc = access.resolve_credential("unipile")
    assert rc.mode == "org" and rc.key == "UNIPILE_KEY"
    assert rc.config.get("dsn") == "api6.unipile.com:13616"


def test_resolve_credential_platform_has_no_config(monkeypatch):
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(access.db, "get_member_api_key", lambda s, o, p: None)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access, "current_org", lambda s: None)
    monkeypatch.setattr(access.db, "get_active_grant",
                        lambda s, p: {"api_key": "PK", "label": "plat", "daily_quota": None})
    monkeypatch.setattr(access.db, "get_usage_today", lambda s, p: 0)
    rc = access.resolve_credential("unipile", want="auto")
    assert rc.is_platform and rc.mode == "platform" and rc.entity_type is None
    assert rc.config == {}


def test_resolve_credential_byo_skips_platform(monkeypatch):
    # want="byo" : aucun byo posé → lève SANS consulter le grant plateforme.
    _wire(monkeypatch, user=None, group=None, org=None)
    monkeypatch.setattr(access.db, "get_active_grant",
                        lambda s, p: pytest.fail("platform consulté en mode byo"))
    with pytest.raises(McpError):
        access.resolve_credential("unipile", want="byo")
