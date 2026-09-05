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


def purger_delegations_expirees(sub: str) -> int:
    """Efface les jetons de DÉLÉGATION expirés de ce compte.

    ⚠️ Un jeton mort n'a aucune raison de rester : il est inutilisable, et
    l'accumulation est mécanique — un par travail exécuté. Appelée à l'émission
    plutôt que par une tâche de fond : le nettoyage est amorti sur l'usage, et
    il n'y a pas de mécanisme nouveau à faire vivre.

    ⚠️ Ne touche QUE `kind='delegation'` : un jeton d'utilisateur expiré reste
    visible, parce que son propriétaire doit pouvoir constater qu'il a expiré —
    c'est le sien, il l'a créé, sa disparition silencieuse serait une surprise.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_api_tokens WHERE sub = %s AND kind = 'delegation' "
            "AND expires_at IS NOT NULL AND expires_at < NOW()",
            (sub,),
        )
        return cur.rowcount or 0


def create_api_token(sub: str, label: str = "cli", ttl_days: Optional[int] = None,
                     scopes: Optional[dict] = None,
                     ttl_seconds: Optional[int] = None,
                     kind: str = "user") -> str:
    """Génère un token, persiste son hash, renvoie le plaintext une seule fois.

    `ttl_days` : si fourni (>0), le token expire après ce délai et est rejeté
    par `verify_api_token`. None = non-expirant (défaut — token CLI long-lived
    stocké en SOPS). La révocation explicite reste `delete_api_token`.

    `scopes` (cf. `token_scopes.py`) : None = jeton NON PORTÉ, il est le sub.
    Non None = deny-by-default, seul ce que la portée nomme passe — la forme d'un
    jeton confié à une intégration tierce. Validé par `token_scopes.parse` AVANT
    d'arriver ici (le document est stocké tel quel).
    """
    # ⚠️ `upsert_user` CRÉE le compte s'il n'existe pas. Pour un jeton émis au nom
    # d'un tiers (délégation d'un travail programmé), l'existence du compte se
    # vérifie donc AVANT d'appeler ici — sinon on ressusciterait silencieusement
    # un compte supprimé, et on lui délivrerait un accès dans la foulée.
    upsert_user(sub)
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    # ⚠️ `ttl_seconds` gagne sur `ttl_days` : un jeton de délégation vit le temps
    # d'un bail (quelques minutes), pas d'une journée. Sans lui, le plus court
    # exprimable était 1 jour — soit ~90 fois la durée du besoin, pour un jeton
    # qui porte l'identité de quelqu'un d'autre.
    if ttl_seconds and ttl_seconds > 0:
        expires = f"NOW() + INTERVAL '{int(ttl_seconds)} seconds'"
    elif ttl_days and ttl_days > 0:
        expires = f"NOW() + INTERVAL '{int(ttl_days)} days'"
    else:
        expires = "NULL"
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO user_api_tokens (sub, label, token_hash, expires_at, "
            f"scopes, kind) VALUES (%s, %s, %s, {expires}, %s, %s)",
            (sub, label, _hash_token(token),
             json.dumps(scopes) if scopes is not None else None, kind),
        )
    return token


def verify_api_token(token: str) -> Optional[dict]:
    """Renvoie `{sub, scopes}` du token, et met à jour last_used_at — `scopes` None
    pour un jeton non porté. None (tout court) si inconnu ou expiré.

    UN SEUL statement (`UPDATE … RETURNING`), et pas un SELECT puis un UPDATE dans la
    même transaction : la forme en deux temps prenait AccessShareLock (SELECT) puis
    demandait RowExclusiveLock (UPDATE) sur `user_api_tokens`. Un `ALTER TABLE` de
    migration (init_db) qui se glissait ENTRE les deux se mettait en file d'attente
    derrière l'AccessShareLock, et l'UPDATE — bloqué derrière l'ALTER en attente
    (file FIFO des locks PG) — fermait le cycle : DeadlockDetected des deux côtés
    (Sentry, 5 événements côté boot + 2 côté requête, dernier 2026-07-30). Un seul
    statement prend son lock d'un coup : plus d'escalade intra-transaction, plus de
    cycle possible avec le DDL.
    """
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None
    h = _hash_token(token)
    with _connect() as conn:
        row = conn.execute(
            "UPDATE user_api_tokens SET last_used_at = NOW() "
            "WHERE token_hash = %s AND (expires_at IS NULL OR expires_at > NOW()) "
            "RETURNING id, sub, scopes, kind",
            (h,),
        ).fetchone()
        if not row:
            return None
        # `id` et `kind` servent le JOURNAL : nommer le jeton employé sans jamais
        # l'écrire. Deux appels du même compte par deux jetons différents étaient
        # jusqu'ici indistinguables — et un jeton de délégation ressemblait à une
        # session humaine.
        return {"sub": row["sub"], "scopes": _as_scopes(row.get("scopes")),
                "token_id": row["id"], "token_kind": row.get("kind")}


def _as_scopes(raw: object) -> Optional[dict]:
    """`scopes` JSONB → dict. La row factory rend les JSONB en dict ; un pilote qui
    rendrait du texte ne doit pas faire d'un jeton PORTÉ un jeton libre — d'où le
    parse explicite, et le **fail-closed** (illisible ⇒ portée vide ⇒ rien ne passe)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def list_api_tokens(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            # ⚠️ `kind = 'user'` : cet écran annonce des jetons de CLI et
            # d'intégration continue. Les jetons de délégation — 12 minutes,
            # émis automatiquement, un par travail — n'y ont pas leur place :
            # ils feraient mentir l'écran, et son bouton « révoquer » porterait
            # sur un accès en cours d'usage.
            "SELECT id, label, created_at, last_used_at, expires_at, scopes "
            "FROM user_api_tokens WHERE sub = %s AND kind = 'user' "
            "ORDER BY created_at DESC",
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
