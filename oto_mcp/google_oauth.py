"""Google OAuth — web flow, per-user, tokens persistés en SQLite.

Flow :
1. User authentifié (Logto JWT) appelle `GET /api/google/oauth/start` →
   on renvoie une URL Google avec un `state` HMAC-signé contenant son `sub`.
2. User redirigé vers Google, consent, redirect vers
   `/api/google/oauth/callback?code=…&state=…`.
3. On vérifie le state, échange le code contre refresh+access token,
   persiste dans le coffre chiffré (`connector_credentials`, connector='google').

Pour utiliser les credentials (côté tools datastore) : `credentials_for(sub)`
charge depuis SQLite, refresh transparent si expiré, renvoie un
`google.oauth2.credentials.Credentials` valide.

Setup ops :
- Env `GOOGLE_WORKSPACE_CLIENT_ID` + `GOOGLE_WORKSPACE_CLIENT_SECRET` —
  OAuth client de type **Web application** dans Google Cloud Console,
  redirect URI `https://mcp.oto.ninja/api/google/oauth/callback`.
- Env `OTO_MCP_PUBLIC_URL` (déjà utilisée pour Logto) — base pour le
  redirect URI ; en local on peut override pour pointer sur localhost.
- Env `OTO_MCP_OAUTH_STATE_SECRET` — secret HMAC pour signer le state
  anti-CSRF (générer avec `python -c 'import secrets; print(secrets.token_urlsafe(32))'`).
"""
from __future__ import annotations

import hmac
import hashlib
import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from . import db


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # Drive COMPLET (restricted) — gérer TOUS les fichiers du user (pas seulement
    # ceux créés par oto). Couvre aussi l'export datastore (#29). Supersede drive.file.
    "https://www.googleapis.com/auth/drive",
    # Gmail surface complète (read/send/reply/draft/archive/trash). Scope
    # RESTRICTED chez Google → audit CASA requis si l'écran de consentement
    # passe en published/external (OK en mode testing).
    "https://www.googleapis.com/auth/gmail.modify",
    # Google Tasks (read/write). Scope SENSIBLE (pas restricted comme Gmail) →
    # vérification Google requise si l'app passe en published, pas d'audit CASA.
    "https://www.googleapis.com/auth/tasks",
    # Google Calendar (read/write events). Scope SENSIBLE (pas restricted) →
    # vérification de marque à la publication, pas d'audit CASA.
    "https://www.googleapis.com/auth/calendar",
    # Google Chat (RESTRICTED) — lire les espaces + lire/poster des messages.
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages",
]

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_STATE_TTL = 600  # 10 min


def _client_id() -> str:
    v = os.environ.get("GOOGLE_WORKSPACE_CLIENT_ID")
    if not v:
        raise RuntimeError("GOOGLE_WORKSPACE_CLIENT_ID env var manquante")
    return v


def _client_secret() -> str:
    v = os.environ.get("GOOGLE_WORKSPACE_CLIENT_SECRET")
    if not v:
        raise RuntimeError("GOOGLE_WORKSPACE_CLIENT_SECRET env var manquante")
    return v


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


def _redirect_uri() -> str:
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/google/oauth/callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_state(sub: str) -> str:
    """HMAC-signed state : `<b64(payload)>.<b64(sig)>` — payload = {sub, ts}."""
    payload = json.dumps({"sub": sub, "ts": int(time.time())}, separators=(",", ":")).encode()
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_state(state: str) -> Optional[str]:
    """Renvoie le sub si state valide et non expiré, sinon None."""
    if not state or "." not in state:
        return None
    p_b64, sig_b64 = state.split(".", 1)
    try:
        payload = _b64url_decode(p_b64)
        sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    expected = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(time.time()) - int(data.get("ts", 0)) > _STATE_TTL:
        return None
    sub = data.get("sub")
    return sub if isinstance(sub, str) else None


def build_auth_url(sub: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        # consent → force refresh_token ; select_account → laisse l'user choisir
        # quel compte Google connecter (clé du multi-compte).
        "prompt": "consent select_account",
        "state": make_state(sub),
        "include_granted_scopes": "true",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Échange le code OAuth contre tokens. Renvoie le dict de réponse Google.

    Clés attendues : `access_token`, `refresh_token`, `expires_in`, `scope`.
    """
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _fetch_email(access_token: str) -> str:
    """Récupère l'adresse du compte Google qui vient de consentir.

    Via le profil Gmail (scope gmail.modify déjà accordé) — évite d'ajouter
    un scope identité juste pour connaître l'email.
    """
    import requests
    r = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    r.raise_for_status()
    email = r.json().get("emailAddress")
    if not email:
        raise RuntimeError("Profil Gmail sans emailAddress — impossible d'identifier le compte.")
    return email


def persist_token(sub: str, token_response: dict) -> str:
    """Persiste les tokens et renvoie l'email du compte Google connecté."""
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        # `build_auth_url` impose `prompt=consent` + `access_type=offline`,
        # donc Google DOIT émettre un refresh_token. Si on arrive ici, c'est
        # un problème côté Google → on remonte plutôt que de masquer.
        raise RuntimeError(
            "Google n'a pas émis de refresh_token malgré prompt=consent. "
            "Vérifie la config du client OAuth dans GCP."
        )
    access_token = token_response.get("access_token")
    expires_in = int(token_response.get("expires_in", 0) or 0)
    expires_at = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat() if expires_in else None
    scopes = token_response.get("scope") or " ".join(SCOPES)
    email = _fetch_email(access_token)
    db.set_google_oauth(
        sub,
        google_email=email,
        refresh_token=refresh_token,
        scopes=scopes,
        access_token=access_token,
        expires_at=expires_at,
    )
    return email


def _refresh_access_token(refresh_token: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def credentials_for(sub: str, account: Optional[str] = None):
    """Renvoie un `google.oauth2.credentials.Credentials` valide pour ce sub.

    `account` (email) cible un compte précis ; None = compte par défaut.
    Charge depuis la DB, refresh transparent si access_token absent ou expiré.
    Lève RuntimeError actionnable si pas de compte connecté.
    """
    row = db.get_google_oauth(sub, account=account)
    if not row:
        suffix = f" pour {account}" if account else ""
        raise RuntimeError(
            f"Aucun compte Google connecté{suffix}. Connecte-le sur "
            "https://app.oto.ninja/ (section Google)."
        )

    from google.oauth2.credentials import Credentials

    access_token = row.get("access_token")
    expires_at = row.get("expires_at")
    needs_refresh = not access_token
    if not needs_refresh and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            # 60s d'avance pour éviter de cracher en plein appel
            if exp.timestamp() - time.time() < 60:
                needs_refresh = True
        except Exception:
            needs_refresh = True

    if needs_refresh:
        resp = _refresh_access_token(row["refresh_token"])
        access_token = resp["access_token"]
        expires_in = int(resp.get("expires_in", 0) or 0)
        new_exp = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat()
        db.update_google_access_token(sub, row.get("google_email"), access_token, new_exp)

    return Credentials(
        token=access_token,
        refresh_token=row["refresh_token"],
        token_uri=_TOKEN_URL,
        client_id=_client_id(),
        client_secret=_client_secret(),
        scopes=row["scopes"].split() if row.get("scopes") else SCOPES,
    )


def list_accounts(sub: str) -> list[dict]:
    """Liste les comptes Google connectés du user (email, défaut, scopes)."""
    return db.list_google_accounts(sub)


def revoke(sub: str, account: Optional[str] = None) -> None:
    """Révoque côté Google + supprime de la DB.

    `account` (email) cible un compte ; None révoque tous les comptes du user.
    """
    import requests

    if account is None:
        rows = db.list_google_accounts(sub)
        targets = [r.get("google_email") for r in rows]
    else:
        targets = [account]

    for email in targets:
        # Révoquer côté Google est best-effort : un credential indéchiffrable
        # (ligne chiffrée avec une master key périmée → InvalidTag) ne doit PAS
        # empêcher la suppression. Le contrat de revoke = supprimer en DB.
        try:
            row = db.get_google_oauth(sub, account=email)
        except Exception:
            row = None
        if row and row.get("refresh_token"):
            try:
                requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": row["refresh_token"]},
                    timeout=10,
                )
            except Exception:
                pass  # on supprime quand même en DB
    db.delete_google_oauth(sub, account=account)
