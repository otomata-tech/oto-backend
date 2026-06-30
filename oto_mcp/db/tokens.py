"""Tokens API (auth CLI).

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from ._conn import _connect
from .users import upsert_user


_TOKEN_PREFIX = "oto_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(sub: str, label: str = "cli", ttl_days: Optional[int] = None) -> str:
    """Génère un token, persiste son hash, renvoie le plaintext une seule fois.

    `ttl_days` : si fourni (>0), le token expire après ce délai et est rejeté
    par `verify_api_token`. None = non-expirant (défaut — token CLI long-lived
    stocké en SOPS). La révocation explicite reste `delete_api_token`.
    """
    upsert_user(sub)
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires = f"NOW() + INTERVAL '{int(ttl_days)} days'" if ttl_days and ttl_days > 0 else "NULL"
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO user_api_tokens (sub, label, token_hash, expires_at) "
            f"VALUES (%s, %s, %s, {expires})",
            (sub, label, _hash_token(token)),
        )
    return token


def verify_api_token(token: str) -> Optional[str]:
    """Renvoie le sub du token, et met à jour last_used_at. None si inconnu ou expiré."""
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None
    h = _hash_token(token)
    with _connect() as conn:
        row = conn.execute(
            "SELECT sub FROM user_api_tokens "
            "WHERE token_hash = %s AND (expires_at IS NULL OR expires_at > NOW())",
            (h,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE user_api_tokens SET last_used_at = NOW() WHERE token_hash = %s",
            (h,),
        )
        return row["sub"]


def list_api_tokens(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, label, created_at, last_used_at, expires_at FROM user_api_tokens WHERE sub = %s ORDER BY created_at DESC",
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_api_token(sub: str, token_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_api_tokens WHERE sub = %s AND id = %s",
            (sub, token_id),
        )
        return cur.rowcount > 0
