"""Instructions plateforme éditables (#50, blocs A/B).

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


def get_platform_instruction(key: str) -> Optional[dict]:
    """Le bloc plateforme `key` ('secret_sauce'|'onboarding') ou None s'il n'a
    jamais été seedé. `{key, body_md, updated_at, updated_by}`."""
    with _connect() as conn:
        return conn.execute(
            "SELECT key, body_md, updated_at, updated_by FROM platform_instructions WHERE key = %s",
            (key,),
        ).fetchone()


def list_platform_instructions() -> list[dict]:
    """Tous les blocs plateforme (surface admin)."""
    with _connect() as conn:
        return list(conn.execute(
            "SELECT key, body_md, updated_at, updated_by FROM platform_instructions ORDER BY key"
        ).fetchall())


def set_platform_instruction(key: str, body_md: str, updated_by: Optional[str] = None) -> None:
    """Upsert d'un bloc plateforme (édition admin)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO platform_instructions (key, body_md, updated_at, updated_by) "
            "VALUES (%s, %s, NOW(), %s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "body_md = EXCLUDED.body_md, updated_at = NOW(), updated_by = EXCLUDED.updated_by",
            (key, body_md, updated_by),
        )


def seed_platform_instruction(key: str, body_md: str) -> None:
    """Pose le défaut d'un bloc plateforme s'il n'existe pas encore (boot, idempotent).
    Ne touche PAS un bloc déjà édité par l'admin."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO platform_instructions (key, body_md) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (key, body_md),
        )
