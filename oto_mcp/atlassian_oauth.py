"""Atlassian OAuth — flow web per-user pour fédérer le Rovo Remote MCP (#40).

Atlassian héberge un MCP distant (mcp.atlassian.com/v1/mcp, Jira + Confluence)
avec SON PROPRE serveur d'autorisation OAuth 2.1 (RFC 8414), DCR + PKCE.
Découverte live (2026-06-22, `.well-known/oauth-authorization-server`) :
  authorization_endpoint : https://mcp.atlassian.com/v1/authorize
  token_endpoint         : https://cf.mcp.atlassian.com/v1/token
  registration_endpoint  : https://cf.mcp.atlassian.com/v1/register  (DCR)
  PKCE S256, grants authorization_code + refresh_token.

Client **public** : la DCR rend `token_endpoint_auth_method=none` (pas de
client_secret) → échange/refresh en `client_id` + `code_verifier`, SANS Basic
auth (≠ memento qui est confidentiel). Le `client_id` est enregistré une fois par
DCR et fourni via `ATLASSIAN_OAUTH_CLIENT_ID`. La sélection du site Atlassian
Cloud (cloudid) est gérée par l'AS Atlassian — rien à porter côté oto.

Comme memento/google : le refresh_token (long-lived) est le `secret` chiffré du
coffre ; l'access_token (bearer ~1h, dérivé) vit dans `meta` et est rafraîchi de
façon transparente. Le proxy de tools/mount.py l'injecte par requête
(access.resolve_mount_token → access_token_for).
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

from . import credentials_store, oauth2_pkce

_AUTH_URL = "https://mcp.atlassian.com/v1/authorize"
_TOKEN_URL = "https://cf.mcp.atlassian.com/v1/token"
_MCP_RESOURCE = "https://mcp.atlassian.com/v1/mcp"
_CONNECTOR = "atlassian"
_STATE_TTL = 600  # 10 min
# offline_access = condition du refresh_token. Les scopes d'outils (jira/confluence)
# sont consentis via l'AS Atlassian ; surchargeable une fois le flow validé en live.
_DEFAULT_SCOPE = "offline_access"


def _client_id() -> str:
    v = os.environ.get("ATLASSIAN_OAUTH_CLIENT_ID")
    if not v:
        raise RuntimeError(
            "ATLASSIAN_OAUTH_CLIENT_ID env var manquante (enregistre un client DCR "
            "sur https://cf.mcp.atlassian.com/v1/register avec le redirect_uri oto)."
        )
    return v


def _scope() -> str:
    return os.environ.get("ATLASSIAN_OAUTH_SCOPE", _DEFAULT_SCOPE)


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


def _redirect_uri() -> str:
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/atlassian/oauth/callback"


def build_auth_url(sub: str) -> str:
    from urllib.parse import urlencode
    verifier, challenge = oauth2_pkce.pkce_pair()
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": _scope(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": oauth2_pkce.make_state(_state_secret(), sub, verifier),
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def verify_state(state: str) -> Optional[tuple[str, str]]:
    """(sub, code_verifier) si le state est valide et non expiré, sinon None."""
    return oauth2_pkce.verify_state(_state_secret(), state, _STATE_TTL)


# --- échange + persistance --------------------------------------------------

def exchange_code(code: str, verifier: str) -> dict:
    import requests
    # Client public → pas de Basic auth ; `client_id` + `code_verifier` dans le corps.
    r = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "code_verifier": verifier,
            "client_id": _client_id(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def persist_token(sub: str, token_response: dict) -> None:
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "Atlassian n'a pas émis de refresh_token (vérifie le scope offline_access).")
    credentials_store.set_credential(
        "user", sub, _CONNECTOR, secret=refresh_token, set_by=sub,
        meta={
            "access_token": token_response.get("access_token"),
            "expires_at": oauth2_pkce.expires_at(token_response.get("expires_in")),
        },
    )


class AtlassianReauthRequired(Exception):
    """Refresh token Atlassian mort (invalid_grant) → l'user doit reconnecter."""


def _refresh(refresh_token: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": _client_id()},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    # Refresh révoqué/expiré → 400/401 invalid_grant : réauth à faire, pas un 5xx.
    if r.status_code in (400, 401):
        raise AtlassianReauthRequired((r.text or "")[:300])
    r.raise_for_status()
    return r.json()


def access_token_for(sub: str) -> Optional[str]:
    """Access token Atlassian valide pour ce sub (refresh transparent si expiré),
    ou None si le user n'a pas connecté Atlassian."""
    cred = credentials_store.get_credential_with_meta("user", sub, _CONNECTOR)
    if not cred or not cred.get("secret"):
        return None
    meta = cred.get("meta") or {}
    access_token = meta.get("access_token")
    expires_at = meta.get("expires_at")
    needs_refresh = not access_token
    if not needs_refresh and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp.timestamp() - time.time() < 60:  # 60s d'avance
                needs_refresh = True
        except Exception:
            needs_refresh = True
    if needs_refresh:
        try:
            resp = _refresh(cred["secret"])
        except AtlassianReauthRequired:
            # Grant mort : purge → status_for repasse « non connecté », l'UI reconnecte.
            credentials_store.clear_credential("user", sub, _CONNECTOR)
            return None
        access_token = resp["access_token"]
        new_refresh = resp.get("refresh_token", cred["secret"])  # rotation possible
        credentials_store.set_credential(
            "user", sub, _CONNECTOR, secret=new_refresh, set_by=sub,
            meta={"access_token": access_token,
                  "expires_at": oauth2_pkce.expires_at(resp.get("expires_in"))},
        )
    return access_token


def status_for(sub: str) -> dict:
    cred = credentials_store.get_credential_with_meta("user", sub, _CONNECTOR)
    return {"connected": bool(cred and cred.get("secret")),
            "set_at": cred.get("set_at") if cred else None}


def disconnect(sub: str) -> bool:
    return credentials_store.clear_credential("user", sub, _CONNECTOR)
