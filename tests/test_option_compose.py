"""`_set_option` (ADR 0024) : « accorder l'option » compose la couche option
(comp `has_option`) ET la couche clé (grant de clé plateforme) pour un connecteur
en mode plateforme — sinon état mort (has_option=true sans clé → 404 au connect).

On monkeypatch db/org_store/connectors pour vérifier chaque branche sans DB.
"""
import types

import pytest

from oto_mcp.capabilities import users_admin as ua
from oto_mcp.capabilities._types import ResolvedCtx

CTX = ResolvedCtx(sub="admin", org_id=1)


def _con(auth_modes):
    return types.SimpleNamespace(auth_modes=frozenset(auth_modes))


@pytest.fixture
def calls(monkeypatch):
    """Capture les écritures ; defaults inertes. ADR 0044 §F : le grant plateforme passe
    par credentials_store.platform_grant/revoke (scope `user:<sub>`|`org:<id>`), plus les
    db.*_platform_key legacy."""
    rec = {"comp": [], "clear": [], "grant": [], "revoke": [], "instances": []}
    monkeypatch.setattr(ua.db, "get_user", lambda eid: {"sub": eid})
    monkeypatch.setattr(ua.org_store, "get_org", lambda oid: {"id": oid})
    monkeypatch.setattr(ua.db, "set_option_comp",
                        lambda et, eid, opt, granted_by=None: rec["comp"].append((et, eid, opt)))
    monkeypatch.setattr(ua.db, "clear_option_comp",
                        lambda et, eid, opt: rec["clear"].append((et, eid, opt)))
    monkeypatch.setattr(ua.credentials_store, "platform_grant",
                        lambda prov, scope, daily_quota=None: rec["grant"].append((prov, scope)))
    monkeypatch.setattr(ua.credentials_store, "platform_revoke",
                        lambda prov, scope: rec["revoke"].append((prov, scope)))
    monkeypatch.setattr(ua.credentials_store, "list_platform_instances", lambda p: rec["instances"])
    monkeypatch.setattr(ua.db, "has_member_api_key", lambda sub, org, prov: False)
    monkeypatch.setattr(ua.org_store, "get_active_org", lambda sub: 1)
    monkeypatch.setattr(ua.org_store, "has_org_secret", lambda oid, prov: False)
    return rec


def test_platform_option_grants_key(calls, monkeypatch):
    """Connecteur mode plateforme + clé posée → comp ET grant de la clé plateforme."""
    monkeypatch.setattr(ua.connectors, "connector_for_provider",
                        lambda p: _con({"byo_user", "platform"}))
    calls["instances"] = [{"label": "env"}]
    out = ua._set_option(CTX, ua.OptionInput(entity_type="user", entity_id="u1",
                                             option="unipile", on=True))
    assert calls["comp"] == [("user", "u1", "unipile")]
    assert calls["grant"] == [("unipile", "user:u1")]
    assert out["platform_key"] == {"granted": True, "provider": "unipile"}


def test_platform_option_no_key_is_flagged(calls, monkeypatch):
    """Connecteur revente SANS clé plateforme posée → comp mais état mort signalé."""
    monkeypatch.setattr(ua.connectors, "connector_for_provider",
                        lambda p: _con({"platform"}))
    # calls["instances"] reste [] (aucune clé plateforme posée)
    out = ua._set_option(CTX, ua.OptionInput(entity_type="user", entity_id="u1",
                                             option="unipile", on=True))
    assert calls["grant"] == []
    assert out["platform_key"]["granted"] is False
    assert out["platform_key"]["reason"] == "no_platform_key"


def test_byo_option_is_inert(calls, monkeypatch):
    """L'entité a sa propre clé (BYO) → grant posé mais signalé inerte."""
    monkeypatch.setattr(ua.connectors, "connector_for_provider",
                        lambda p: _con({"byo_user", "platform"}))
    calls["instances"] = [{"label": "env"}]
    monkeypatch.setattr(ua.db, "has_member_api_key", lambda sub, org, prov: True)
    out = ua._set_option(CTX, ua.OptionInput(entity_type="user", entity_id="u1",
                                             option="unipile", on=True))
    assert out["platform_key"]["byo_inert"] is True


def test_non_platform_option_is_plain_comp(calls, monkeypatch):
    """Option non liée à un connecteur plateforme → comp simple, aucun grant."""
    monkeypatch.setattr(ua.connectors, "connector_for_provider", lambda p: None)
    out = ua._set_option(CTX, ua.OptionInput(entity_type="org", entity_id="3",
                                             option="some_addon", on=True))
    assert calls["comp"] == [("org", "3", "some_addon")]
    assert calls["grant"] == []
    assert out["platform_key"] is None


def test_remove_option_revokes_grant(calls, monkeypatch):
    """Retirer la comp d'un connecteur plateforme retire aussi le grant (symétrie)."""
    monkeypatch.setattr(ua.connectors, "connector_for_provider",
                        lambda p: _con({"platform"}))
    calls["instances"] = [{"label": "env"}]
    out = ua._set_option(CTX, ua.OptionInput(entity_type="user", entity_id="u1",
                                             option="unipile", on=False))
    assert calls["clear"] == [("user", "u1", "unipile")]
    assert calls["revoke"] == [("unipile", "user:u1")]
    assert out["platform_key"] == {"revoked": True}


def test_org_scope_grants_org_key(calls, monkeypatch):
    monkeypatch.setattr(ua.connectors, "connector_for_provider",
                        lambda p: _con({"platform"}))
    calls["instances"] = [{"label": "env"}]
    out = ua._set_option(CTX, ua.OptionInput(entity_type="org", entity_id="5",
                                             option="unipile", on=True))
    assert calls["grant"] == [("unipile", "org:5")]
    assert out["platform_key"]["granted"] is True
