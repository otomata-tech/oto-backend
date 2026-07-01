"""Folk OAuth — flow web per-user pour fédérer le MCP officiel de Folk (#85).

Folk héberge un MCP distant (mcp.folk.app/mcp) dont le serveur d'autorisation est
**Stytch** (RFC 9728). Découverte live (2026-07-01,
`api.stytch.folk.app/.well-known/oauth-authorization-server`) :
  authorization_endpoint : https://app.folk.app/oauth/authorize
  token_endpoint         : https://api.stytch.folk.app/v1/oauth2/token
  registration_endpoint  : https://api.stytch.folk.app/v1/oauth2/register  (DCR)
  PKCE S256, grants authorization_code + refresh_token, `none` supporté.

Client **public** (comme atlassian) : DCR `token_endpoint_auth_method=none` (pas de
client_secret) → échange/refresh en `client_id` + `code_verifier`, sans Basic auth.
Le `client_id` est auto-enregistré une fois (cache coffre plateforme) ou fourni via
`FOLK_OAUTH_CLIENT_ID`.

Le MCP de Folk s'auth UNIQUEMENT par OAuth (pas de clé API — c'est le connecteur
natif `folk` qui, lui, tape l'API REST à la clé). Ce module sert donc le connecteur
fédéré `folkmcp`, distinct et coexistant (per-user visibility, ADR 0011/0031).

Comme atlassian/memento : le refresh_token (long-lived) est le `secret` chiffré du
coffre ; l'access_token (bearer, dérivé) vit dans `meta`, rafraîchi de façon
transparente. Le proxy de tools/mount.py l'injecte par requête
(access.resolve_mount_token → access_token_for).

⚠️ À confirmer au 1ᵉʳ consent live : le **scope** exact requis par le MCP Folk pour
lire/écrire le workspace (`full_access` déclaré côté openid-configuration ;
défaut ci-dessous, surchargeable par `FOLK_OAUTH_SCOPE`).
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

from . import credentials_store, oauth2_pkce

_AUTH_URL = "https://app.folk.app/oauth/authorize"
_TOKEN_URL = "https://api.stytch.folk.app/v1/oauth2/token"
_REGISTER_URL = "https://api.stytch.folk.app/v1/oauth2/register"
# Identifiant de ressource RFC 8707/9728 (valeur exacte du PRM de mcp.folk.app).
_MCP_RESOURCE = "https://mcp.folk.app"
_CONNECTOR = "folkmcp"
# Entité « plateforme » du coffre où l'on cache le client_id DCR (public, pas un
# secret) — auto-enregistré une fois, partagé par tous les users.
_CLIENT_ENTITY = ("platform", "")
_STATE_TTL = 600  # 10 min
# offline_access = condition du refresh_token ; openid = base OIDC ; full_access =
# accès workspace (déclaré côté openid-configuration). Surchargeable une fois le
# flow validé en live (le MCP peut exiger un scope différent).
_DEFAULT_SCOPE = "openid offline_access full_access"


def _scope() -> str:
    return os.environ.get("FOLK_OAUTH_SCOPE", _DEFAULT_SCOPE)


def _register_client() -> str:
    """DCR d'un client PUBLIC sur l'AS Stytch (token_endpoint_auth_method=none,
    pas de secret) → renvoie le client_id. Le redirect_uri DOIT matcher au callback."""
    import requests
    r = requests.post(_REGISTER_URL, json={
        "client_name": "Oto",
        "redirect_uris": [_redirect_uri()],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": _scope(),
    }, timeout=15)
    r.raise_for_status()
    cid = r.json().get("client_id")
    if not cid:
        raise RuntimeError("DCR Folk (Stytch) sans client_id")
    return cid


def _client_id() -> str:
    """client_id OAuth — ZÉRO env requise. Override `FOLK_OAUTH_CLIENT_ID` si posée ;
    sinon cache coffre (entité plateforme) ; sinon **auto-DCR** (client public) puis
    cache. La DB gouverne — pas de provisioning manuel."""
    env = os.environ.get("FOLK_OAUTH_CLIENT_ID")
    if env:
        return env
    cached = credentials_store.get_credential(*_CLIENT_ENTITY, _CONNECTOR)
    if cached:
        return cached
    cid = _register_client()
    credentials_store.set_credential(*_CLIENT_ENTITY, _CONNECTOR, secret=cid, set_by="system")
    return cid


def reset_client_id() -> None:
    """Purge le client_id caché → re-DCR au prochain `_client_id()`. À appeler si
    l'AS rejette le client (`invalid_client` : registration purgée côté Stytch)."""
    credentials_store.clear_credential(*_CLIENT_ENTITY, _CONNECTOR)


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


def _redirect_uri() -> str:
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/folk/oauth/callback"


def build_auth_url(sub: str) -> str:
    from urllib.parse import urlencode
    verifier, challenge = oauth2_pkce.pkce_pair()
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": _scope(),
        "resource": _MCP_RESOURCE,
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
            "resource": _MCP_RESOURCE,
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
            "Folk (Stytch) n'a pas émis de refresh_token (vérifie le scope offline_access).")
    credentials_store.set_credential(
        "user", sub, _CONNECTOR, secret=refresh_token, set_by=sub,
        meta={
            "access_token": token_response.get("access_token"),
            "expires_at": oauth2_pkce.expires_at(token_response.get("expires_in")),
        },
    )


class FolkReauthRequired(Exception):
    """Refresh token Folk mort (invalid_grant) → l'user doit reconnecter."""


def _refresh(refresh_token: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": _client_id(), "resource": _MCP_RESOURCE},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    # Refresh révoqué/expiré → 400/401 invalid_grant : réauth à faire, pas un 5xx.
    if r.status_code in (400, 401):
        raise FolkReauthRequired((r.text or "")[:300])
    r.raise_for_status()
    return r.json()


def access_token_for(sub: str) -> Optional[str]:
    """Access token Folk valide pour ce sub (refresh transparent si expiré), ou None
    si le user n'a pas connecté le MCP Folk."""
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
        except FolkReauthRequired:
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
