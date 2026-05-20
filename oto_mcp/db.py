"""PostgreSQL-backed user store (Scaleway managed `otomata-main`).

One row per Logto user (`sub` = primary key). Holds per-user settings :
- `role` — guest / member / admin (cf. `access.py` pour les implications)
- LinkedIn / Crunchbase session cookies + user-agent
- API keys par provider (serper/hunter/sirene/attio/lemlist) — plaintext,
  isolation par ACL réseau + creds en SOPS.
- Compteur d'usage `usage(sub, tool, day)` pour les quotas member.

Connexion via `DATABASE_URL` (postgresql://…?sslmode=require). Pool psycopg
géré au module ; toutes les fonctions restent sync.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _normalize_value(v: Any) -> Any:
    # Match the string shape SQLite returned ("YYYY-MM-DD HH:MM:SS") so downstream
    # JSONResponse + frontends keep working unchanged.
    if isinstance(v, datetime):
        return v.replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _str_dict_row(cursor):
    inner = dict_row(cursor)

    def make_row(values):
        d = inner(values)
        if d:
            for k, v in d.items():
                if isinstance(v, (datetime, date)):
                    d[k] = _normalize_value(v)
        return d

    return make_row


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    sub TEXT PRIMARY KEY,
    email TEXT,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'guest',
    linkedin_cookie TEXT,
    linkedin_user_agent TEXT,
    linkedin_cookie_set_at TIMESTAMPTZ,
    serper_api_key TEXT,
    hunter_api_key TEXT,
    sirene_api_key TEXT,
    attio_api_key TEXT,
    lemlist_api_key TEXT,
    crunchbase_cookies TEXT,
    crunchbase_user_agent TEXT,
    crunchbase_set_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage (
    sub TEXT NOT NULL,
    tool TEXT NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sub, tool, day)
);

CREATE TABLE IF NOT EXISTS user_disabled_tools (
    sub TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, tool_name)
);

CREATE TABLE IF NOT EXISTS platform_keys (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    api_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, label)
);

CREATE TABLE IF NOT EXISTS user_grants (
    sub TEXT NOT NULL,
    platform_key_id BIGINT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by TEXT,
    PRIMARY KEY (sub, platform_key_id),
    FOREIGN KEY (platform_key_id) REFERENCES platform_keys(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_google_oauth (
    sub TEXT PRIMARY KEY,
    refresh_token TEXT NOT NULL,
    access_token TEXT,
    expires_at TIMESTAMPTZ,
    scopes TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_datastores (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT NOT NULL,
    namespace TEXT NOT NULL,
    spreadsheet_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(sub, namespace)
);

CREATE TABLE IF NOT EXISTS user_api_tokens (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT 'cli',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_user_api_tokens_sub ON user_api_tokens(sub);
"""

# Providers supportés pour les user keys. Aligné sur les colonnes
# `<provider>_api_key` ci-dessus et sur `oto.config.get_secret(<UPPER>_API_KEY)`.
KEY_PROVIDERS = ("serper", "hunter", "sirene", "attio", "lemlist")


_pool: Optional[ConnectionPool] = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (managed PG connection string)")
    return url


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_database_url(),
            min_size=1,
            max_size=int(os.environ.get("OTO_MCP_DB_POOL_MAX", "8")),
            kwargs={"row_factory": _str_dict_row},
            open=True,
        )
    return _pool


@contextmanager
def _connect() -> Iterator[psycopg.Connection]:
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_SCHEMA)


def upsert_user(sub: str, email: Optional[str] = None, name: Optional[str] = None) -> None:
    """Create the user row if missing, refresh email/name if known."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (sub, email, name)
            VALUES (%s, %s, %s)
            ON CONFLICT(sub) DO UPDATE SET
                email = COALESCE(EXCLUDED.email, users.email),
                name  = COALESCE(EXCLUDED.name,  users.name),
                updated_at = NOW()
            """,
            (sub, email, name),
        )


def get_user(sub: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE sub = %s", (sub,)).fetchone()
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
            "UPDATE users SET role = %s, updated_at = NOW() WHERE sub = %s",
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
               SET linkedin_cookie = %s,
                   linkedin_user_agent = COALESCE(%s, linkedin_user_agent),
                   linkedin_cookie_set_at = NOW(),
                   updated_at = NOW()
             WHERE sub = %s
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
                   updated_at = NOW()
             WHERE sub = %s
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


# --- Crunchbase -------------------------------------------------------------

def set_crunchbase_session(
    sub: str,
    cookies_json: str,
    user_agent: Optional[str] = None,
) -> None:
    """Store cookies (JSON-encoded list) + UA. Couple cookies + UA doit
    matcher le browser d'origine pour réduire le risque de blocage.
    """
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
               SET crunchbase_cookies = %s,
                   crunchbase_user_agent = COALESCE(%s, crunchbase_user_agent),
                   crunchbase_set_at = NOW(),
                   updated_at = NOW()
             WHERE sub = %s
            """,
            (cookies_json, user_agent, sub),
        )


def clear_crunchbase_session(sub: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
               SET crunchbase_cookies = NULL,
                   crunchbase_user_agent = NULL,
                   crunchbase_set_at = NULL,
                   updated_at = NOW()
             WHERE sub = %s
            """,
            (sub,),
        )


def get_crunchbase_session(sub: str) -> Optional[dict]:
    """Renvoie `{cookies: list[dict], user_agent: str|None}` ou None."""
    import json as _json
    user = get_user(sub)
    if not user or not user.get("crunchbase_cookies"):
        return None
    try:
        cookies = _json.loads(user["crunchbase_cookies"])
    except Exception:
        return None
    return {
        "cookies": cookies,
        "user_agent": user.get("crunchbase_user_agent"),
    }


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
            f"UPDATE users SET {col} = %s, updated_at = NOW() WHERE sub = %s",
            (key, sub),
        )


def clear_user_api_key(sub: str, provider: str) -> None:
    _check_provider(provider)
    col = f"{provider}_api_key"
    with _connect() as conn:
        conn.execute(
            f"UPDATE users SET {col} = NULL, updated_at = NOW() WHERE sub = %s",
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
        row = conn.execute(
            """
            INSERT INTO usage (sub, tool, day, count)
            VALUES (%s, %s, CURRENT_DATE, 1)
            ON CONFLICT(sub, tool, day) DO UPDATE SET count = usage.count + 1
            RETURNING count
            """,
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0


# --- per-user disabled tools ------------------------------------------------

def list_user_disabled_tools(sub: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool_name FROM user_disabled_tools WHERE sub = %s ORDER BY tool_name",
            (sub,),
        ).fetchall()
        return [r["tool_name"] for r in rows]


def is_tool_disabled_for(sub: str, tool_name: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 AS x FROM user_disabled_tools WHERE sub = %s AND tool_name = %s",
            (sub, tool_name),
        ).fetchone()
        return row is not None


def add_user_disabled_tool(sub: str, tool_name: str) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_disabled_tools (sub, tool_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (sub, tool_name),
        )


def remove_user_disabled_tool(sub: str, tool_name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_disabled_tools WHERE sub = %s AND tool_name = %s",
            (sub, tool_name),
        )


def get_usage_today(sub: str, tool: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE sub = %s AND tool = %s AND day = CURRENT_DATE",
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
        sql += " WHERE provider = %s"
        params = (provider,)
    sql += " ORDER BY provider, created_at"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_platform_key(key_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, provider, label, api_key, created_at FROM platform_keys WHERE id = %s",
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
            row = conn.execute(
                "INSERT INTO platform_keys (provider, label, api_key) VALUES (%s, %s, %s) RETURNING id",
                (provider, label, api_key),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"({provider}, {label}) existe déjà") from e
        return int(row["id"])


def upsert_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée ou met à jour la clé pour (provider, label). Idempotent — utilisé
    par le bootstrap des env vars au démarrage.
    """
    _check_provider(provider)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO platform_keys (provider, label, api_key)
            VALUES (%s, %s, %s)
            ON CONFLICT(provider, label) DO UPDATE SET api_key = EXCLUDED.api_key
            RETURNING id
            """,
            (provider, label, api_key),
        ).fetchone()
        return int(row["id"])


def delete_platform_key(key_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM platform_keys WHERE id = %s", (key_id,))


# --- grants -----------------------------------------------------------------

def grant_platform_key(sub: str, platform_key_id: int, granted_by: Optional[str] = None) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_grants (sub, platform_key_id, granted_at, granted_by)
            VALUES (%s, %s, NOW(), %s)
            ON CONFLICT(sub, platform_key_id) DO UPDATE SET
                granted_at = NOW(),
                granted_by = EXCLUDED.granted_by
            """,
            (sub, platform_key_id, granted_by),
        )


def revoke_platform_key(sub: str, platform_key_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_grants WHERE sub = %s AND platform_key_id = %s",
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
             WHERE ug.sub = %s
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
             WHERE ug.sub = %s AND pk.provider = %s
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


# --- Google OAuth -----------------------------------------------------------

def set_google_oauth(
    sub: str,
    refresh_token: str,
    scopes: str,
    access_token: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_google_oauth (sub, refresh_token, access_token, expires_at, scopes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(sub) DO UPDATE SET
                refresh_token = EXCLUDED.refresh_token,
                access_token  = EXCLUDED.access_token,
                expires_at    = EXCLUDED.expires_at,
                scopes        = EXCLUDED.scopes,
                updated_at    = NOW()
            """,
            (sub, refresh_token, access_token, expires_at, scopes),
        )


def update_google_access_token(sub: str, access_token: str, expires_at: str) -> None:
    """Met à jour uniquement l'access_token + expiry (sur refresh)."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE user_google_oauth
               SET access_token = %s, expires_at = %s, updated_at = NOW()
             WHERE sub = %s
            """,
            (access_token, expires_at, sub),
        )


def get_google_oauth(sub: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_google_oauth WHERE sub = %s", (sub,)
        ).fetchone()
        return dict(row) if row else None


def delete_google_oauth(sub: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM user_google_oauth WHERE sub = %s", (sub,))


# --- Datastore namespaces ---------------------------------------------------

def create_datastore_namespace(sub: str, namespace: str, spreadsheet_id: str) -> int:
    upsert_user(sub)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO user_datastores (sub, namespace, spreadsheet_id) VALUES (%s, %s, %s) RETURNING id",
                (sub, namespace, spreadsheet_id),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"namespace `{namespace}` existe déjà") from e
        return int(row["id"])


def get_datastore_namespace(sub: str, namespace: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_datastores WHERE sub = %s AND namespace = %s",
            (sub, namespace),
        ).fetchone()
        return dict(row) if row else None


def list_datastore_namespaces(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace, spreadsheet_id, created_at FROM user_datastores WHERE sub = %s ORDER BY namespace",
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_datastore_namespace(sub: str, namespace: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_datastores WHERE sub = %s AND namespace = %s",
            (sub, namespace),
        )
        return cur.rowcount > 0


# --- API tokens (CLI auth) --------------------------------------------------

_TOKEN_PREFIX = "oto_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(sub: str, label: str = "cli") -> str:
    """Génère un token, persiste son hash, renvoie le plaintext une seule fois."""
    upsert_user(sub)
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_api_tokens (sub, label, token_hash) VALUES (%s, %s, %s)",
            (sub, label, _hash_token(token)),
        )
    return token


def verify_api_token(token: str) -> Optional[str]:
    """Renvoie le sub du token, et met à jour last_used_at. None si inconnu."""
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None
    h = _hash_token(token)
    with _connect() as conn:
        row = conn.execute(
            "SELECT sub FROM user_api_tokens WHERE token_hash = %s", (h,)
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
            "SELECT id, label, created_at, last_used_at FROM user_api_tokens WHERE sub = %s ORDER BY created_at DESC",
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
