"""Tests de la validation d'écriture d'un secret partagé d'org.

`connectors.org_secret_meta` est la fonction de service réutilisée par l'adaptateur
REST `api_routes_orgs` (PUT /api/orgs/{id}/secrets/{provider}) ET miroir des règles
de `oto_admin_set_org_secret` (MCP). Pure (registre seul) → testable sans DB/crypto.
"""
import pytest

from oto_mcp import connectors


def test_shareable_provider_no_base_url_ok():
    meta, code = connectors.org_secret_meta("serper", None)
    assert code is None
    assert meta is None  # provider normal : pas de satellite


def test_per_user_provider_refused():
    # linkedin/google/whatsapp = sessions perso → jamais partageables.
    for provider in ("linkedin", "google", "whatsapp"):
        meta, code = connectors.org_secret_meta(provider, None)
        assert code == "provider_not_shareable", provider
        assert meta is None


def test_slack_now_org_shareable():
    # slack = BYO configurable par org/user (#25, byo_org) → partageable comme
    # serper (un workspace partagé par l'org = son bot token).
    meta, code = connectors.org_secret_meta("slack", None)
    assert code is None
    assert meta is None


def test_unknown_provider_refused():
    meta, code = connectors.org_secret_meta("does-not-exist", None)
    assert code == "provider_not_shareable"
    assert meta is None


def test_data_driven_remote_with_base_url_ok_and_stripped():
    # Remote data-driven (ADR 0003/0011) : un base_url définit un bridge, sans
    # entrée registre ni nom client en dur. Trailing slash retiré.
    meta, code = connectors.org_secret_meta("some-bridge", "https://bridge.example.com/")
    assert code is None
    assert meta == {"base_url": "https://bridge.example.com"}


def test_unknown_provider_without_base_url_not_remote():
    # Sans base_url, un provider inconnu n'est PAS un remote → refusé.
    meta, code = connectors.org_secret_meta("some-bridge", None)
    assert code == "provider_not_shareable"
    assert meta is None


def test_normal_connector_rejects_base_url():
    meta, code = connectors.org_secret_meta("attio", "https://nope.example.com")
    assert code == "base_url_not_allowed"
    assert meta is None


def test_all_org_shareable_providers_accepted_without_base_url_unless_remote():
    remote = {c.name for c in connectors.REMOTE_CONNECTORS}
    for provider in connectors.ORG_SHAREABLE_PROVIDERS:
        meta, code = connectors.org_secret_meta(provider, None)
        if provider in remote:
            assert code == "base_url_required", provider
        else:
            assert code is None, provider
