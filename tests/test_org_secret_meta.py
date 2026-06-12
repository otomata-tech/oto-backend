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
    # slack/linkedin/google/whatsapp = sessions perso → jamais partageables.
    for provider in ("slack", "linkedin", "google", "whatsapp"):
        meta, code = connectors.org_secret_meta(provider, None)
        assert code == "provider_not_shareable", provider
        assert meta is None


def test_unknown_provider_refused():
    meta, code = connectors.org_secret_meta("does-not-exist", None)
    assert code == "provider_not_shareable"
    assert meta is None


def test_remote_connector_requires_base_url():
    # mm = connecteur remote → base_url (endpoint du bridge) obligatoire.
    meta, code = connectors.org_secret_meta("mm", None)
    assert code == "base_url_required"
    assert meta is None


def test_remote_connector_with_base_url_ok_and_stripped():
    meta, code = connectors.org_secret_meta("mm", "https://bridge.example.com/")
    assert code is None
    assert meta == {"base_url": "https://bridge.example.com"}  # trailing slash retiré


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
