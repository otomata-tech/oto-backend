"""SQLite-backed user store.

One row per Logto user (`sub` = primary key). Holds per-user settings :
- `role` — guest / member / admin (cf. `access.py` pour les implications)
- LinkedIn cookie + user-agent
- API keys par provider (serper/hunter/sirene) — chiffrement none, simple
  storage. La DB est sur disque local, accès root only.
- Compteur d'usage `usage(sub, tool, day)` pour les quotas member.

Path: `OTO_MCP_DB_PATH` env (default `/opt/oto-mcp/data/oto-mcp.sqlite` en
prod, `./data/oto-mcp.sqlite` en dev). Directory created on first init.
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
    role TEXT NOT NULL DEFAULT 'guest',
    linkedin_cookie TEXT,
    linkedin_user_agent TEXT,
    linkedin_cookie_set_at TEXT,
    serper_api_key TEXT,
    hunter_api_key TEXT,
    sirene_api_key TEXT,
    attio_api_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS usage (
    sub TEXT NOT NULL,
    tool TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sub, tool, day)
);

-- Plusieurs platform keys par provider possibles (perso, pro, client X…) ;
-- l'admin les gère via /api/admin/platform-keys.
CREATE TABLE IF NOT EXISTS platform_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    api_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, label)
);

-- Grants explicites par (user, platform_key). Un user peut avoir 0..N grants
-- pour un même provider — à la résolution on prend le plus récemment granté.
CREATE TABLE IF NOT EXISTS user_grants (
    sub TEXT NOT NULL,
    platform_key_id INTEGER NOT NULL,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    granted_by TEXT,
    PRIMARY KEY (sub, platform_key_id),
    FOREIGN KEY (platform_key_id) REFERENCES platform_keys(id) ON DELETE CASCADE
);
"""

# Migrations idempotentes — ajout de colonnes sur bases existantes.
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN linkedin_user_agent TEXT",
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'guest'",
    "ALTER TABLE users ADD COLUMN serper_api_key TEXT",
    "ALTER TABLE users ADD COLUMN hunter_api_key TEXT",
    "ALTER TABLE users ADD COLUMN sirene_api_key TEXT",
    "ALTER TABLE users ADD COLUMN attio_api_key TEXT",
]

# Providers supportés pour les user keys. Aligné sur les colonnes
# `<provider>_api_key` ci-dessus et sur `oto.config.get_secret(<UPPER>_API_KEY)`.
KEY_PROVIDERS = ("serper", "hunter", "sirene", "attio")


def db_path() -> Path:
    raw = os.environ.get("OTO_MCP_DB_PATH") or _DEFAULT_PATH
    p = Path(raw).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
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


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, email, name, role, created_at, updated_at FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


# --- role -------------------------------------------------------------------

def set_user_role(sub: str, role: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET role = ?, updated_at = datetime('now') WHERE sub = ?",
            (role, sub),
        )


# --- LinkedIn ---------------------------------------------------------------

def set_linkedin_cookie(sub: str, cookie: str, user_agent: Optional[str] = None) -> None:
    """Store/refresh le cookie li_at + UA d'un user. Le couple cookie + UA
    doit matcher le browser d'origine pour réduire le risque de ban.
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
    user = get_user(sub)
    if not user or not user.get("linkedin_cookie"):
        return None
    return {
        "cookie": user["linkedin_cookie"],
        "user_agent": user.get("linkedin_user_agent"),
    }


def get_linkedin_cookie(sub: str) -> Optional[str]:
    user = get_user(sub)
    return user.get("linkedin_cookie") if user else None


# --- user API keys ----------------------------------------------------------

def _check_provider(provider: str) -> None:
    if provider not in KEY_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (allowed: {KEY_PROVIDERS})")


def set_user_api_key(sub: str, provider: str, key: str) -> None:
    _check_provider(provider)
    upsert_user(sub)
    col = f"{provider}_api_key"
    with _connect() as conn:
        conn.execute(
            f"UPDATE users SET {col} = ?, updated_at = datetime('now') WHERE sub = ?",
            (key, sub),
        )


def clear_user_api_key(sub: str, provider: str) -> None:
    _check_provider(provider)
    col = f"{provider}_api_key"
    with _connect() as conn:
        conn.execute(
            f"UPDATE users SET {col} = NULL, updated_at = datetime('now') WHERE sub = ?",
            (sub,),
        )


def get_user_api_key(sub: str, provider: str) -> Optional[str]:
    _check_provider(provider)
    user = get_user(sub)
    if not user:
        return None
    return user.get(f"{provider}_api_key")


# --- usage counters ---------------------------------------------------------

def increment_usage(sub: str, tool: str) -> int:
    """Incrémente le compteur (sub, tool, today). Retourne la nouvelle valeur."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO usage (sub, tool, day, count)
            VALUES (?, ?, date('now'), 1)
            ON CONFLICT(sub, tool, day) DO UPDATE SET count = count + 1
            """,
            (sub, tool),
        )
        row = conn.execute(
            "SELECT count FROM usage WHERE sub = ? AND tool = ? AND day = date('now')",
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0


def get_usage_today(sub: str, tool: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE sub = ? AND tool = ? AND day = date('now')",
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0


# --- platform keys (admin-managed) ------------------------------------------

def list_platform_keys(provider: Optional[str] = None) -> list[dict]:
    """Liste les platform keys. **Inclut `api_key`** — réservé à l'admin
    backend, jamais retourné via /api (la route admin masque ce champ).
    """
    sql = "SELECT id, provider, label, api_key, created_at FROM platform_keys"
    params: tuple = ()
    if provider:
        sql += " WHERE provider = ?"
        params = (provider,)
    sql += " ORDER BY provider, created_at"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_platform_key(key_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, provider, label, api_key, created_at FROM platform_keys WHERE id = ?",
            (key_id,),
        ).fetchone()
        return dict(row) if row else None


def create_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée une platform key. Renvoie l'id ; lève ValueError sur (provider, label) duplicata."""
    _check_provider(provider)
    if not label or not api_key:
        raise ValueError("label et api_key requis")
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO platform_keys (provider, label, api_key) VALUES (?, ?, ?)",
                (provider, label, api_key),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"({provider}, {label}) existe déjà") from e
        return int(cur.lastrowid)


def upsert_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée ou met à jour la clé pour (provider, label). Idempotent — utilisé
    par le bootstrap des env vars au démarrage.
    """
    _check_provider(provider)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO platform_keys (provider, label, api_key)
            VALUES (?, ?, ?)
            ON CONFLICT(provider, label) DO UPDATE SET api_key = excluded.api_key
            """,
            (provider, label, api_key),
        )
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = conn.execute(
            "SELECT id FROM platform_keys WHERE provider = ? AND label = ?",
            (provider, label),
        ).fetchone()
        return int(row["id"])


def delete_platform_key(key_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM platform_keys WHERE id = ?", (key_id,))


# --- grants -----------------------------------------------------------------

def grant_platform_key(sub: str, platform_key_id: int, granted_by: Optional[str] = None) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_grants (sub, platform_key_id, granted_at, granted_by)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(sub, platform_key_id) DO UPDATE SET
                granted_at = datetime('now'),
                granted_by = excluded.granted_by
            """,
            (sub, platform_key_id, granted_by),
        )


def revoke_platform_key(sub: str, platform_key_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_grants WHERE sub = ? AND platform_key_id = ?",
            (sub, platform_key_id),
        )


def list_grants_for_user(sub: str) -> list[dict]:
    """Grants détaillés d'un user — joint platform_keys pour ne pas exposer
    l'api_key brut côté API. Renvoie id/provider/label/granted_at."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.provider, pk.label, ug.granted_at, ug.granted_by
              FROM user_grants ug
              JOIN platform_keys pk ON pk.id = ug.platform_key_id
             WHERE ug.sub = ?
             ORDER BY pk.provider, ug.granted_at DESC
            """,
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_grant(sub: str, provider: str) -> Optional[dict]:
    """Grant à utiliser pour ce (user, provider) — le plus récemment granté
    s'il y en a plusieurs. Renvoie {platform_key_id, label, api_key} ou None.
    """
    _check_provider(provider)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.label, pk.api_key
              FROM user_grants ug
              JOIN platform_keys pk ON pk.id = ug.platform_key_id
             WHERE ug.sub = ? AND pk.provider = ?
             ORDER BY ug.granted_at DESC
             LIMIT 1
            """,
            (sub, provider),
        ).fetchone()
        return dict(row) if row else None


def list_users_with_grants() -> list[dict]:
    """Pour /api/admin/users — chaque user + ses grants (sans api_key)."""
    users = list_users()
    out = []
    for u in users:
        u = dict(u)
        u["grants"] = list_grants_for_user(u["sub"])
        out.append(u)
    return out
