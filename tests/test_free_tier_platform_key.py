"""Free-tier clé plateforme (ADR 0031) : un connecteur `platform_key_open` résout
la clé plateforme SANS grant, avec quota gratuit par user (`default_quota`). N'est
atteint qu'en l'absence de toute clé BYO (la cascade est testée ailleurs).
"""
import pytest

from oto_mcp import access


@pytest.fixture
def _no_byo_no_grant(monkeypatch):
    """Aucune clé BYO (user/group/org), aucun grant → on tombe sur le platform path."""
    monkeypatch.setattr(access, "require_connector_access", lambda p, s=None: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.db, "get_active_grant", lambda sub, p: None)
    monkeypatch.setattr(access.db, "get_active_org_grant", lambda org, p: None)
    yield


def test_free_tier_resolves_platform_key_under_quota(_no_byo_no_grant, monkeypatch):
    # serper : platform_key_open=True, default_quota=200.
    monkeypatch.setattr(access.db, "get_platform_api_key",
                        lambda p: {"api_key": "PLAT", "label": "free"})
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: 0)
    rc = access.resolve_credential("serper", sub="u")
    assert rc.key == "PLAT"
    assert rc.is_platform is True


def test_free_tier_quota_exceeded_raises(_no_byo_no_grant, monkeypatch):
    monkeypatch.setattr(access.db, "get_platform_api_key",
                        lambda p: {"api_key": "PLAT", "label": "free"})
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: 200)  # = quota serper
    with pytest.raises(Exception):  # McpError « quota dépassé »
        access.resolve_credential("serper", sub="u")


def test_free_tier_absent_platform_key_raises(_no_byo_no_grant, monkeypatch):
    # platform_key_open mais aucune clé plateforme posée en DB → erreur actionnable.
    monkeypatch.setattr(access.db, "get_platform_api_key", lambda p: None)
    with pytest.raises(Exception):
        access.resolve_credential("serper", sub="u")
