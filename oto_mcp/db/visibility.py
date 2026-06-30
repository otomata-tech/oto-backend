"""Visibilité d'outils per-(sub, org) : disabled/enabled overrides + presets nommés (ADR 0015).

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


def list_user_disabled_tools(sub: str, org_id: int = 0) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool_name FROM user_disabled_tools WHERE sub = %s AND org_id = %s ORDER BY tool_name",
            (sub, org_id),
        ).fetchall()
        return [r["tool_name"] for r in rows]


def is_tool_disabled_for(sub: str, tool_name: str, org_id: int = 0) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 AS x FROM user_disabled_tools WHERE sub = %s AND org_id = %s AND tool_name = %s",
            (sub, org_id, tool_name),
        ).fetchone()
        return row is not None


def add_user_disabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (sub, org_id, tool_name),
        )


def remove_user_disabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_disabled_tools WHERE sub = %s AND org_id = %s AND tool_name = %s",
            (sub, org_id, tool_name),
        )


def replace_user_disabled_tools(sub: str, tool_names: list[str], org_id: int = 0) -> None:
    """Remplace l'ensemble des disabled_tools du profil (sub, org_id) par celui passé.

    Utilisé par `apply_user_preset` pour basculer en un appel atomique.
    """
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_disabled_tools WHERE sub = %s AND org_id = %s", (sub, org_id))
            if tool_names:
                conn.executemany(
                    "INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s)",
                    [(sub, org_id, t) for t in tool_names],
                )


def list_user_enabled_tools(sub: str, org_id: int = 0) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool_name FROM user_enabled_tools WHERE sub = %s AND org_id = %s ORDER BY tool_name",
            (sub, org_id),
        ).fetchall()
        return [r["tool_name"] for r in rows]


def add_user_enabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (sub, org_id, tool_name),
        )


def remove_user_enabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_enabled_tools WHERE sub = %s AND org_id = %s AND tool_name = %s",
            (sub, org_id, tool_name),
        )


def replace_user_enabled_tools(sub: str, tool_names: list[str], org_id: int = 0) -> None:
    """Remplace l'ensemble des enabled-overrides du profil (sub, org_id)."""
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_enabled_tools WHERE sub = %s AND org_id = %s", (sub, org_id))
            if tool_names:
                conn.executemany(
                    "INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s)",
                    [(sub, org_id, t) for t in tool_names],
                )


def list_user_presets(sub: str, org_id: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, enabled_tools, updated_at FROM user_presets "
            "WHERE sub = %s AND org_id = %s ORDER BY name",
            (sub, org_id),
        ).fetchall()
        return [
            {
                "name": r["name"],
                "enabled_tools": list(r["enabled_tools"] or []),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]


def get_user_preset(sub: str, name: str, org_id: int = 0) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, enabled_tools, updated_at FROM user_presets "
            "WHERE sub = %s AND org_id = %s AND name = %s",
            (sub, org_id, name),
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "enabled_tools": list(row["enabled_tools"] or []),
            "updated_at": row["updated_at"],
        }


def save_user_preset(sub: str, name: str, enabled_tools: list[str], org_id: int = 0) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_presets (sub, org_id, name, enabled_tools) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sub, org_id, name) DO UPDATE SET "
            "enabled_tools = EXCLUDED.enabled_tools, updated_at = NOW()",
            (sub, org_id, name, enabled_tools),
        )


def delete_user_preset(sub: str, name: str, org_id: int = 0) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_presets WHERE sub = %s AND org_id = %s AND name = %s",
            (sub, org_id, name),
        )
        return (cur.rowcount or 0) > 0
