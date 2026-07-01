"""folk_oauth — flow web PKCE per-user pour le MCP fédéré `folkmcp` (#85).

Verrouille la construction de l'URL d'autorisation Stytch (endpoint, PKCE S256,
resource indicator RFC 8707, redirect) et l'aller-retour du state HMAC-signé.
Aucun réseau, aucune DB : `FOLK_OAUTH_CLIENT_ID` court-circuite le DCR.
"""
import os
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")
os.environ.setdefault("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja")
os.environ.setdefault("FOLK_OAUTH_CLIENT_ID", "cid-test")

from oto_mcp import folk_oauth  # noqa: E402


def _auth_params(sub="sub-123"):
    u = urlparse(folk_oauth.build_auth_url(sub))
    return u, parse_qs(u.query)


def test_authorize_endpoint_is_folk_app():
    u, _ = _auth_params()
    assert u.netloc == "app.folk.app"
    assert u.path == "/oauth/authorize"


def test_pkce_and_resource_indicator():
    _, q = _auth_params()
    assert q["code_challenge_method"][0] == "S256"
    assert q["code_challenge"][0]  # présent, non vide
    # RFC 8707 : la ressource ciblée = l'identité du MCP Folk (valeur du PRM).
    assert q["resource"][0] == "https://mcp.folk.app"


def test_redirect_uri_and_client_id():
    _, q = _auth_params()
    assert q["redirect_uri"][0] == "https://mcp.oto.ninja/api/folk/oauth/callback"
    assert q["client_id"][0] == "cid-test"


def test_scope_requests_offline_access():
    _, q = _auth_params()
    # offline_access = condition du refresh_token (sinon persist_token lève).
    assert "offline_access" in q["scope"][0]


def test_state_roundtrip_recovers_sub_and_verifier():
    _, q = _auth_params("sub-xyz")
    state = q["state"][0]
    got = folk_oauth.verify_state(state)
    assert got is not None
    sub, verifier = got
    assert sub == "sub-xyz"
    # le verifier du state correspond au challenge de l'URL (S256)
    from oto_mcp import oauth2_pkce
    assert oauth2_pkce.b64url(
        __import__("hashlib").sha256(verifier.encode()).digest()
    ) == q["code_challenge"][0]


def test_tampered_state_is_rejected():
    _, q = _auth_params()
    state = q["state"][0]
    payload, sig = state.split(".", 1)
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + sig
    assert folk_oauth.verify_state(tampered) is None


def test_persist_token_requires_refresh_token():
    with pytest.raises(RuntimeError, match="refresh_token"):
        folk_oauth.persist_token("sub-1", {"access_token": "at-only"})
