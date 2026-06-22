"""Helpers OAuth2 + PKCE génériques (state HMAC-signé, paire PKCE S256, expiry).

Sans dépendance à un connecteur — partagés par les flows web fédérés d'oto.

Le `code_verifier` PKCE est porté DANS le `state` HMAC-signé (intégrité, pas
confidentialité) plutôt que stocké côté serveur. Acceptable pour les flows web
d'oto, y compris **client public** (Atlassian) : le `redirect_uri` est HTTPS et
contrôlé par oto — le code d'autorisation arrive directement au serveur, pas via
un custom-scheme interceptable comme une app native — donc PKCE reste effectif.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Optional


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge S256)."""
    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def make_state(secret: bytes, sub: str, verifier: str) -> str:
    """State opaque {sub, code_verifier, ts}, HMAC-signé avec `secret`."""
    payload = json.dumps({"sub": sub, "v": verifier, "ts": int(time.time())},
                         separators=(",", ":")).encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{b64url(payload)}.{b64url(sig)}"


def verify_state(secret: bytes, state: str, ttl: int) -> Optional[tuple[str, str]]:
    """(sub, code_verifier) si signature valide ET non expiré (< ttl s), sinon None."""
    if not state or "." not in state:
        return None
    p_b64, sig_b64 = state.split(".", 1)
    try:
        payload, sig = b64url_decode(p_b64), b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(sig, hmac.new(secret, payload, hashlib.sha256).digest()):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(time.time()) - int(data.get("ts", 0)) > ttl:
        return None
    sub, v = data.get("sub"), data.get("v")
    return (sub, v) if isinstance(sub, str) and isinstance(v, str) else None


def expires_at(expires_in) -> Optional[str]:
    """ISO 8601 UTC de l'expiration d'un access token, depuis `expires_in` (s)."""
    n = int(expires_in or 0)
    if not n:
        return None
    return datetime.fromtimestamp(time.time() + n, tz=timezone.utc).isoformat()
