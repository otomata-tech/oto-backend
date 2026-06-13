"""Memento OAuth — flow web per-user (otomata#16, B2).

Memento est un MCP autonome (mcp.mento.cc/mcp) authentifié OAuth 2.1 + DCR via
Supabase. Pour le **fédérer** dans oto (connecteur `kind="mount"`, cf.
tools/mount.py), chaque user oto connecte SON compte memento : oto agit en client
OAuth confidentiel (enregistré par DCR, creds en SOPS `MEMENTO_OAUTH_CLIENT_*`),
stocke le token per-user dans le coffre, et le proxy l'injecte par requête.

Flow (calqué sur google_oauth.py, + PKCE requis par Supabase) :
1. `/api/memento/oauth/start` (auth Logto) → URL authorize Supabase avec un
   `state` HMAC-signé portant {sub, code_verifier} + le code_challenge S256.
2. L'user consent sur memento → Supabase redirige vers
   `/api/memento/oauth/callback?code=…&state=…`.
3. On vérifie le state, échange le code (client_secret_basic + code_verifier)
   contre {access_token, refresh_token, expires_in}, persiste dans le coffre
   (`connector_credentials`, connector='memento', entity=user).
4. `access_token_for(sub)` : lit le coffre, refresh transparent si expiré.

Le refresh_token (sensible, long-lived) est le `secret` chiffré du coffre ;
l'access_token (bearer ~1h, dérivé) vit dans `meta` — même modèle que Google.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from . import connectors, credentials_store

_AS = "https://doebdriroupduqpggcsj.supabase.co/auth/v1"
_AUTH_URL = f"{_AS}/oauth/authorize"
_TOKEN_URL = f"{_AS}/oauth/token"
_MCP_RESOURCE = "https://mcp.mento.cc/mcp"
_CONNECTOR = "memento"
_STATE_TTL = 600  # 10 min


def _client_id() -> str:
    v = os.environ.get("MEMENTO_OAUTH_CLIENT_ID")
    if not v:
        raise RuntimeError("MEMENTO_OAUTH_CLIENT_ID env var manquante")
    return v


def _client_secret() -> str:
    v = os.environ.get("MEMENTO_OAUTH_CLIENT_SECRET")
    if not v:
        raise RuntimeError("MEMENTO_OAUTH_CLIENT_SECRET env var manquante")
    return v


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


def _redirect_uri() -> str:
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/memento/oauth/callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- PKCE + state (verifier embarqué dans le state HMAC-signé) --------------

def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def make_state(sub: str, verifier: str) -> str:
    payload = json.dumps({"sub": sub, "v": verifier, "ts": int(time.time())},
                         separators=(",", ":")).encode()
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_state(state: str) -> Optional[tuple[str, str]]:
    """Renvoie (sub, code_verifier) si valide+non expiré, sinon None."""
    if not state or "." not in state:
        return None
    p_b64, sig_b64 = state.split(".", 1)
    try:
        payload, sig = _b64url_decode(p_b64), _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(sig, hmac.new(_state_secret(), payload, hashlib.sha256).digest()):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(time.time()) - int(data.get("ts", 0)) > _STATE_TTL:
        return None
    sub, v = data.get("sub"), data.get("v")
    return (sub, v) if isinstance(sub, str) and isinstance(v, str) else None


def build_auth_url(sub: str) -> str:
    from urllib.parse import urlencode
    verifier, challenge = _pkce_pair()
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": make_state(sub, verifier),
        "resource": _MCP_RESOURCE,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


# --- échange + persistance --------------------------------------------------

def _basic_auth() -> str:
    return base64.b64encode(f"{_client_id()}:{_client_secret()}".encode()).decode()


def exchange_code(code: str, verifier: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "code_verifier": verifier,
            "resource": _MCP_RESOURCE,
        },
        headers={"Authorization": f"Basic {_basic_auth()}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _expires_at(expires_in) -> Optional[str]:
    n = int(expires_in or 0)
    if not n:
        return None
    return datetime.fromtimestamp(time.time() + n, tz=timezone.utc).isoformat()


def persist_token(sub: str, token_response: dict) -> None:
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Memento n'a pas émis de refresh_token (vérifie scope/offline).")
    credentials_store.set_credential(
        "user", sub, _CONNECTOR, secret=refresh_token, set_by=sub,
        meta={
            "access_token": token_response.get("access_token"),
            "expires_at": _expires_at(token_response.get("expires_in")),
        },
    )


def _refresh(refresh_token: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Authorization": f"Basic {_basic_auth()}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def access_token_for(sub: str) -> Optional[str]:
    """Access token memento valide pour ce sub (refresh transparent si expiré),
    ou None si le user n'a pas connecté memento."""
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
        resp = _refresh(cred["secret"])
        access_token = resp["access_token"]
        new_refresh = resp.get("refresh_token", cred["secret"])  # Supabase rotate parfois
        credentials_store.set_credential(
            "user", sub, _CONNECTOR, secret=new_refresh, set_by=sub,
            meta={"access_token": access_token, "expires_at": _expires_at(resp.get("expires_in"))},
        )
    return access_token


def status_for(sub: str) -> dict:
    cred = credentials_store.get_credential_with_meta("user", sub, _CONNECTOR)
    return {"connected": bool(cred and cred.get("secret")),
            "set_at": cred.get("set_at") if cred else None}


def disconnect(sub: str) -> bool:
    return credentials_store.clear_credential("user", sub, _CONNECTOR)
