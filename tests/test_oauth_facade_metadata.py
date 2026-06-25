"""Façade DCR — métadonnée AS (RFC 8414).

Verrou de non-régression sur l'incident 2026-06-25 : un client OAuth strict
(Mistral) rejette le discovery si l'`issuer` retourné par l'AS metadata ne
correspond pas EXACTEMENT (au trailing slash près) à l'identifiant d'AS annoncé
dans le PRM `authorization_servers`. Le PRM passe l'URL par `AnyHttpUrl`
(RemoteAuthProvider) qui ajoute un slash final → l'issuer DOIT le faire aussi.
"""
import pytest
from pydantic import AnyHttpUrl

from oto_mcp.oauth_facade import as_metadata, as_oidc_metadata


@pytest.fixture(autouse=True)
def _logto_env(monkeypatch):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")


@pytest.mark.parametrize("public_url", [
    "https://mcp.oto.ninja",      # sans slash (forme OTO_MCP_PUBLIC_URL.rstrip)
    "https://mcp.oto.ninja/",     # avec slash
])
def test_issuer_matches_prm_authorization_server(public_url):
    """L'issuer de l'AS metadata == la normalisation AnyHttpUrl que le PRM
    annonce dans `authorization_servers` (RFC 8414 §3.3), quelle que soit la
    forme de OTO_MCP_PUBLIC_URL."""
    meta = as_metadata(public_url)
    prm_authz_server = str(AnyHttpUrl(public_url))  # ce que RemoteAuthProvider sert
    assert meta["issuer"] == prm_authz_server


def test_oidc_metadata_issuer_matches_and_has_required_fields():
    """OIDC discovery : même issuer normalisé que le PRM + champs OBLIGATOIRES
    OpenID Connect Discovery 1.0 (subject_types / id_token alg)."""
    meta = as_oidc_metadata("https://mcp.oto.ninja")
    assert meta["issuer"] == str(AnyHttpUrl("https://mcp.oto.ninja"))
    assert meta["subject_types_supported"] == ["public"]
    assert meta["id_token_signing_alg_values_supported"] == ["ES384"]  # Logto self-hosted
    assert meta["userinfo_endpoint"] == "https://auth.oto.ninja/oidc/me"


def test_oauth_endpoints_point_to_logto():
    meta = as_metadata("https://mcp.oto.ninja")
    assert meta["authorization_endpoint"] == "https://auth.oto.ninja/oidc/auth"
    assert meta["token_endpoint"] == "https://auth.oto.ninja/oidc/token"
    # le registration_endpoint reste sur NOTRE domaine (façade DCR)
    assert meta["registration_endpoint"] == "https://mcp.oto.ninja/oauth/register"
