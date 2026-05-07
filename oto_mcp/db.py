"""SQLite-backed user store.

One row per Logto user (`sub` = primary key). Holds per-user settings, today
just the LinkedIn cookie. Stdlib sqlite3, no ORM — schema is small enough.

Path: `OTO_MCP_DB_PATH` env (default `/opt/oto-mcp/data/oto-mcp.sqlite` in
prod, `./data/oto-mcp.sqlite` in dev). Directory is created on first init.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


_DEFAULT_PATH = "/opt/oto-mcp/data/oto-mcp.sqlite"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    sub TEXT PRIMARY KEY,
    email TEXT,
    name TEXT,
    linkedin_cookie TEXT,
    linkedin_user_agent TEXT,
    linkedin_cookie_set_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- migration : colonne ajoutée après v0
"""

_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN linkedin_user_agent TEXT",
]


def db_path() -> Path:
    raw = os.environ.get("OTO_MCP_DB_PATH") or _DEFAULT_PATH
    p = Path(raw).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # Migrations idempotentes : on ignore les "duplicate column" sur les bases existantes.
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_user(sub: str, email: Optional[str] = None, name: Optional[str] = None) -> None:
    """Create the user row if missing, refresh email/name if known."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (sub, email, name)
            VALUES (?, ?, ?)
            ON CONFLICT(sub) DO UPDATE SET
                email = COALESCE(excluded.email, users.email),
                name  = COALESCE(excluded.name,  users.name),
                updated_at = datetime('now')
            """,
            (sub, email, name),
        )


def get_user(sub: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()
        return dict(row) if row else None


def set_linkedin_cookie(sub: str, cookie: str, user_agent: Optional[str] = None) -> None:
    """Store/refresh the LinkedIn `li_at` cookie + optional user-agent for a user.

    Le couple cookie + UA doit matcher le browser d'origine — c'est ce qui réduit
    le risque de ban LinkedIn (mismatch cookie ↔ device fingerprint).
    """
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
               SET linkedin_cookie = ?,
                   linkedin_user_agent = COALESCE(?, linkedin_user_agent),
                   linkedin_cookie_set_at = datetime('now'),
                   updated_at = datetime('now')
             WHERE sub = ?
            """,
            (cookie, user_agent, sub),
        )


def clear_linkedin_cookie(sub: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
               SET linkedin_cookie = NULL,
                   linkedin_user_agent = NULL,
                   linkedin_cookie_set_at = NULL,
                   updated_at = datetime('now')
             WHERE sub = ?
            """,
            (sub,),
        )


def get_linkedin_session(sub: str) -> Optional[dict]:
    """Cookie + UA pour passer à LinkedInClient. Retourne None si rien configuré."""
    user = get_user(sub)
    if not user or not user.get("linkedin_cookie"):
        return None
    return {
        "cookie": user["linkedin_cookie"],
        "user_agent": user.get("linkedin_user_agent"),
    }


# Compat : un seul appel utilisé par les tools, alias historique conservé.
def get_linkedin_cookie(sub: str) -> Optional[str]:
    user = get_user(sub)
    return user.get("linkedin_cookie") if user else None
