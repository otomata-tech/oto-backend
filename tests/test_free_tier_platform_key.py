"""Free-tier clé plateforme (ADR 0031) : un connecteur `platform_key_open` résout
la clé plateforme SANS grant, avec quota gratuit par user (`default_quota`). N'est
atteint qu'en l'absence de toute clé BYO (la cascade est testée ailleurs).
"""
import pytest

from oto_mcp import access


@pytest.fixture
def _no_byo_no_grant(monkeypatch):
    """Aucune clé BYO (user/group/org) ni instance plateforme par défaut → chemin plateforme
    (ADR 0044 §F : la clé plateforme est une instance scope PLATFORM du coffre unifié)."""
    monkeypatch.setattr(access, "require_connector_access", lambda p, s=None: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.credentials_store, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, p, account="": "PLAT")
    yield


# Instance free-tier serper : 'open' + share_down vide ⟹ ouverte à tous ; quota meta.rate_limit=200.
_FREE_SERPER = [{"label": "free", "share_mode": "open", "share_down": [],
                 "share_side": [], "meta": {"rate_limit": 200}}]


def test_free_tier_resolves_platform_key_under_quota(_no_byo_no_grant, monkeypatch):
    monkeypatch.setattr(access.credentials_store, "list_platform_instances", lambda p: _FREE_SERPER)
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: 0)
    rc = access.resolve_credential("serper", sub="u")
    assert rc.key == "PLAT"
    assert rc.is_platform is True


def test_free_tier_quota_exceeded_raises(_no_byo_no_grant, monkeypatch):
    monkeypatch.setattr(access.credentials_store, "list_platform_instances", lambda p: _FREE_SERPER)
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: 200)  # = rate_limit
    with pytest.raises(Exception):  # McpError « quota dépassé »
        access.resolve_credential("serper", sub="u")


def test_free_tier_absent_platform_key_raises(_no_byo_no_grant, monkeypatch):
    # platform_key_open mais AUCUNE instance plateforme (fixture: list_platform_instances=[]).
    with pytest.raises(Exception):
        access.resolve_credential("serper", sub="u")
