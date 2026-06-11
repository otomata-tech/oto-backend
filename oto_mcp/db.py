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
import logging
import os
import secrets
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import connectors

logger = logging.getLogger(__name__)


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
-- Identité seule. Les credentials (clés API, sessions linkedin/crunchbase,
-- OAuth Google) vivent TOUS dans le coffre chiffré `connector_credentials`.
CREATE TABLE IF NOT EXISTS users (
    sub TEXT PRIMARY KEY,
    email TEXT,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'guest',
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

-- Journal des appels MCP (monitoring admin). Une ligne par appel de tool,
-- posée par CallMonitoringMiddleware (succès comme échec). Volumétrie bornée
-- par un prune au boot (cf. prune_tool_call_log + init_db). `sub` nullable :
-- les appels stdio local non authentifiés n'ont pas d'identité.
CREATE TABLE IF NOT EXISTS tool_call_log (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT,
    tool_name TEXT NOT NULL,
    called_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms INTEGER,
    ok BOOLEAN NOT NULL DEFAULT TRUE,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_called_at ON tool_call_log(called_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_sub ON tool_call_log(sub);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_tool ON tool_call_log(tool_name);

CREATE TABLE IF NOT EXISTS user_disabled_tools (
    sub TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, tool_name)
);

-- Ensemble positif explicite : tools que l'user a activé alors qu'ils sont
-- masqués par défaut (DEFAULT_HIDDEN_TOOLS). Sans cette table, un tool
-- default-hidden ne pourrait jamais être rendu visible (le modèle de base
-- n'a qu'un ensemble négatif).
CREATE TABLE IF NOT EXISTS user_enabled_tools (
    sub TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    enabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, tool_name)
);

CREATE TABLE IF NOT EXISTS user_presets (
    sub TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled_tools TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, name)
);

CREATE TABLE IF NOT EXISTS platform_keys (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    api_key_enc TEXT,                      -- enveloppe AES-256-GCM (chiffrement obligatoire)
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

-- Grants de namespace sensible (deny-by-default). Un user non-admin ne voit/
-- n'appelle un tool d'un ADMIN_GRANT_ONLY_NAMESPACE que s'il a une ligne ici,
-- posée par un admin. Distinct de user_grants (qui porte une platform_key/quota).
CREATE TABLE IF NOT EXISTS user_namespace_grants (
    sub TEXT NOT NULL,
    namespace TEXT NOT NULL,
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, namespace)
);

CREATE TABLE IF NOT EXISTS user_datastores (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT NOT NULL,
    namespace TEXT NOT NULL,
    spreadsheet_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(sub, namespace)
);

CREATE TABLE IF NOT EXISTS datastore_shares (
    id BIGSERIAL PRIMARY KEY,
    owner_sub TEXT NOT NULL,
    namespace TEXT NOT NULL,
    spreadsheet_id TEXT NOT NULL,
    shared_with_sub TEXT NOT NULL,
    permission TEXT NOT NULL DEFAULT 'write',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(owner_sub, namespace, shared_with_sub)
);
CREATE INDEX IF NOT EXISTS idx_datastore_shares_recipient ON datastore_shares(shared_with_sub, namespace);

CREATE TABLE IF NOT EXISTS user_api_tokens (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT 'cli',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_user_api_tokens_sub ON user_api_tokens(sub);

-- Palier organization (= périmètre / store serveur). Une org possède des
-- credentials propres (coffre `connector_credentials`, entity_type='org'), un
-- set de namespaces autorisés (org_entitlements), et des opérateurs
-- (org_members). Source de vérité de l'appartenance = ces tables, résolues par
-- `sub` — JAMAIS un claim du token
-- Logto (le token MCP ne porte que sub). Cf. project_oto_mcp_org_tier.
-- NB barreau 1 : tables seules, aucun helper ne les lit encore (canari de
-- déploiement). Le câblage (resolve_api_key, visibilité, meta-tools) suit.
CREATE TABLE IF NOT EXISTS orgs (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- org_role : 'org_admin' | 'org_member' (validé en code, pas par CHECK, comme
-- users.role). is_active = org courante du sub (au plus une TRUE par sub,
-- garantie par l'index partiel + l'écriture ; même pattern que
-- user_google_oauth.is_default).
CREATE TABLE IF NOT EXISTS org_members (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    org_role TEXT NOT NULL DEFAULT 'org_member',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, sub)
);
CREATE INDEX IF NOT EXISTS idx_org_members_sub ON org_members(sub);
CREATE UNIQUE INDEX IF NOT EXISTS org_members_one_active ON org_members(sub) WHERE is_active;

-- Les credentials d'org (Attio, Pennylane, MM token…) vivent dans le coffre
-- chiffré `connector_credentials` (entity_type='org'), pas dans une table dédiée.

-- Plafond de visibilité plateforme -> org : généralise
-- ADMIN_GRANT_ONLY_NAMESPACES + user_namespace_grants au niveau org. Débloque
-- un namespace gouverné (mm, gocardless) pour les membres de l'org.
CREATE TABLE IF NOT EXISTS org_entitlements (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, namespace)
);

-- Instructions markdown d'une org : doctrine de base + bibliothèque de skills.
-- Modèle unifié — chaque instruction est identifiée par `slug` ; le slug réservé
-- 'claude_md' = la doctrine de base servie d'office par get_claude_md(), les
-- autres = des skills chargés à la demande (list/search/get). En CLAIR (prose,
-- pas un credential → hors coffre chiffré). Même principe d'accès que les
-- secrets d'org : résolu par l'org active du sub (get_active_org). `version` est
-- incrémenté à chaque écriture, qui archive un snapshot dans la table sœur.
CREATE TABLE IF NOT EXISTS org_instructions (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_org_instructions_org ON org_instructions(org_id);

-- Historique : un snapshot par version posée (revert + audit). Append-only.
CREATE TABLE IF NOT EXISTS org_instruction_revisions (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, slug, version)
);

-- Coffre unique des credentials per-entité (user OU org) : clés API, sessions
-- linkedin/crunchbase, OAuth Google multi-compte, platform keys. entity_id = sub
-- (user) ou org_id::text (org) ; toujours requêter (entity_type, entity_id)
-- ENSEMBLE. Secret chiffré par enveloppe AES-256-GCM dans `secret_enc`
-- (obligatoire — pas de colonne plaintext) ; déchiffrement JIT dans
-- resolve_api_key. meta JSONB pour les satellites (user_agent, scopes…).
CREATE TABLE IF NOT EXISTS connector_credentials (
    entity_type TEXT NOT NULL,            -- 'user' | 'org'
    entity_id   TEXT NOT NULL,            -- users.sub | orgs.id::text
    connector   TEXT NOT NULL,            -- nom de connecteur (registre)
    account     TEXT NOT NULL DEFAULT '', -- discriminant multi-compte ('' = mono ; ex. email Google)
    secret_enc  TEXT,                     -- enveloppe AES-256-GCM (obligatoire)
    secret_kind TEXT NOT NULL DEFAULT 'api_key',
    meta        JSONB NOT NULL DEFAULT '{}',
    set_by      TEXT,
    set_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_type, entity_id, connector, account)
);
CREATE INDEX IF NOT EXISTS idx_conn_cred_entity ON connector_credentials(entity_type, entity_id);
"""

# Providers supportés pour les user keys. DÉRIVÉ du registre source unique
# (`connectors.py`) — ne plus éditer ici, déclarer le connecteur dans le registre.
KEY_PROVIDERS = connectors.KEY_PROVIDERS


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
        # Idempotent column adds — `CREATE TABLE IF NOT EXISTS` ne propage pas les
        # nouvelles colonnes sur les tables existantes.
        conn.execute("ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS daily_quota INTEGER")
        # Coffre chiffré : colonnes courantes (idempotent pour les DB créées avant).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS secret_enc TEXT")
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE connector_credentials DROP CONSTRAINT IF EXISTS connector_credentials_pkey")
        conn.execute("ALTER TABLE connector_credentials ADD PRIMARY KEY (entity_type, entity_id, connector, account)")
        conn.execute("ALTER TABLE platform_keys ADD COLUMN IF NOT EXISTS api_key_enc TEXT")
        _drop_legacy_plaintext_stores(conn)
    # Borne la volumétrie du journal de monitoring (hors transaction schéma).
    try:
        prune_tool_call_log(int(os.environ.get("OTO_MCP_CALL_LOG_RETENTION_DAYS", "30")))
    except Exception as e:
        logger.warning("prune_tool_call_log failed: %s", e)


def _drop_legacy_plaintext_stores(conn: psycopg.Connection) -> None:
    """Purge des emplacements plaintext supersédés par le coffre chiffré
    `connector_credentials` (migration terminée + soak nullé en prod, cf.
    `project_oto_connector_vault`). Idempotent (IF EXISTS) — no-op sur une DB
    fraîche (on-prem). Le chiffrement est désormais obligatoire : plus aucun
    chemin plaintext (writers/reveal en chiffré-seul)."""
    # connector_credentials.secret (plaintext interne du coffre) + platform_keys.api_key
    conn.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS secret")
    conn.execute("ALTER TABLE platform_keys DROP COLUMN IF EXISTS api_key")
    # Colonnes legacy users.<provider>_api_key + sessions linkedin/crunchbase.
    for col in ("serper_api_key", "hunter_api_key", "sirene_api_key", "attio_api_key",
                "lemlist_api_key", "kaspr_api_key", "pennylane_api_key", "slack_api_key",
                "fullenrich_api_key", "linkedin_cookie", "linkedin_user_agent",
                "linkedin_cookie_set_at", "crunchbase_cookies", "crunchbase_user_agent",
                "crunchbase_set_at"):
        conn.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {col}")
    # Tables legacy entièrement foldées dans le coffre.
    conn.execute("DROP TABLE IF EXISTS org_secrets")
    conn.execute("DROP TABLE IF EXISTS user_google_oauth")


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


def get_user_by_email(email: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
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
    """Store/refresh le cookie li_at + UA d'un user. Le couple cookie + UA doit
    matcher le browser d'origine pour réduire le risque de ban.

    Coffre chiffré unique : secret = cookie, UA dans meta. UA effectif résolu
    depuis le coffre si non fourni."""
    upsert_user(sub)
    from . import credentials_store
    ua = user_agent
    if ua is None:
        cur = credentials_store.get_credential_with_meta("user", sub, "linkedin")
        ua = cur["meta"].get("user_agent") if cur else None
    credentials_store.set_credential(
        "user", sub, "linkedin", cookie, set_by=sub, meta={"user_agent": ua})


def clear_linkedin_cookie(sub: str) -> None:
    from . import credentials_store
    credentials_store.clear_credential("user", sub, "linkedin")


def get_linkedin_session(sub: str) -> Optional[dict]:
    """Cutover (folding) : lit le coffre (déchiffre), non plus les colonnes legacy."""
    from . import credentials_store
    cur = credentials_store.get_credential_with_meta("user", sub, "linkedin")
    if not cur or not cur["secret"]:
        return None
    return {
        "cookie": cur["secret"],
        "user_agent": cur["meta"].get("user_agent"),
        "set_at": cur["set_at"],
    }


def get_linkedin_cookie(sub: str) -> Optional[str]:
    from . import credentials_store
    return credentials_store.get_credential("user", sub, "linkedin")


def get_linkedin_status(sub: str) -> Optional[dict]:
    """Statut SANS déchiffrer (pour /api/me) : {set_at, user_agent} ou None."""
    from . import credentials_store
    st = credentials_store.credential_status("user", sub, "linkedin")
    if not st:
        return None
    return {"set_at": st["set_at"], "user_agent": st["meta"].get("user_agent")}


# --- Crunchbase -------------------------------------------------------------

def set_crunchbase_session(
    sub: str,
    cookies_json: str,
    user_agent: Optional[str] = None,
) -> None:
    """Store cookies (JSON-encoded list) + UA. Coffre chiffré unique : secret =
    cookies_json, UA dans meta."""
    upsert_user(sub)
    from . import credentials_store
    ua = user_agent
    if ua is None:
        cur = credentials_store.get_credential_with_meta("user", sub, "crunchbase")
        ua = cur["meta"].get("user_agent") if cur else None
    credentials_store.set_credential(
        "user", sub, "crunchbase", cookies_json, set_by=sub, meta={"user_agent": ua})


def clear_crunchbase_session(sub: str) -> None:
    from . import credentials_store
    credentials_store.clear_credential("user", sub, "crunchbase")


def get_crunchbase_session(sub: str) -> Optional[dict]:
    """Renvoie `{cookies: list[dict], user_agent, set_at}` ou None. Cutover :
    lit le coffre (déchiffre)."""
    import json as _json
    from . import credentials_store
    cur = credentials_store.get_credential_with_meta("user", sub, "crunchbase")
    if not cur or not cur["secret"]:
        return None
    try:
        cookies = _json.loads(cur["secret"])
    except Exception:
        return None
    return {
        "cookies": cookies,
        "user_agent": cur["meta"].get("user_agent"),
        "set_at": cur["set_at"],
    }


def get_crunchbase_status(sub: str) -> Optional[dict]:
    """Statut SANS déchiffrer (pour /api/me) : {set_at, user_agent} ou None."""
    from . import credentials_store
    st = credentials_store.credential_status("user", sub, "crunchbase")
    if not st:
        return None
    return {"set_at": st["set_at"], "user_agent": st["meta"].get("user_agent")}


# --- user API keys ----------------------------------------------------------

def _check_provider(provider: str) -> None:
    if provider not in KEY_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (allowed: {KEY_PROVIDERS})")


def set_user_api_key(sub: str, provider: str, key: str) -> None:
    _check_provider(provider)
    upsert_user(sub)
    # Coffre chiffré, source unique. Import lazy (db ne doit pas importer
    # credentials_store au niveau module — cycle).
    from . import credentials_store
    credentials_store.set_credential("user", sub, provider, key, set_by=sub)


def clear_user_api_key(sub: str, provider: str) -> None:
    _check_provider(provider)
    from . import credentials_store
    credentials_store.clear_credential("user", sub, provider)


def get_user_api_key(sub: str, provider: str) -> Optional[str]:
    # Lit le coffre `connector_credentials` (déchiffre — chemin de RÉSOLUTION).
    # Import lazy (anti-cycle) ; require_keyed dans le store.
    from . import credentials_store
    return credentials_store.get_credential("user", sub, provider)


def has_user_api_key(sub: str, provider: str) -> bool:
    """Présence d'une clé perso SANS la déchiffrer (status_for / /api/me)."""
    from . import credentials_store
    return credentials_store.has_credential("user", sub, provider)


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


# --- MCP call monitoring (journal admin) ------------------------------------

def record_tool_call(
    sub: Optional[str],
    tool_name: str,
    duration_ms: int,
    ok: bool,
    error: Optional[str] = None,
) -> None:
    """Journalise un appel de tool MCP. Best-effort : ne JAMAIS faire échouer
    l'appel pour un problème de logging (l'appelant gère le try/except)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tool_call_log (sub, tool_name, duration_ms, ok, error)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (sub, tool_name, duration_ms, ok, (error or None)),
        )


def list_tool_calls(
    limit: int = 200,
    sub: Optional[str] = None,
    tool_name: Optional[str] = None,
    errors_only: bool = False,
    since_days: Optional[int] = None,
) -> list[dict]:
    """Derniers appels MCP (récent d'abord), joints à l'email user pour l'UI."""
    limit = max(1, min(int(limit), 1000))
    clauses: list[str] = []
    params: list[Any] = []
    if sub:
        clauses.append("l.sub = %s")
        params.append(sub)
    if tool_name:
        clauses.append("l.tool_name = %s")
        params.append(tool_name)
    if errors_only:
        clauses.append("l.ok = FALSE")
    if since_days is not None:
        clauses.append("l.called_at >= NOW() - make_interval(days => %s)")
        params.append(int(since_days))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT l.id, l.sub, u.email, u.name, l.tool_name, l.called_at,
                   l.duration_ms, l.ok, l.error
            FROM tool_call_log l
            LEFT JOIN users u ON u.sub = l.sub
            {where}
            ORDER BY l.called_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def tool_call_stats(since_days: int = 7) -> dict:
    """Agrégats pour le dashboard de monitoring sur les `since_days` derniers jours :
    total, échecs, ventilation par tool / par user / par jour."""
    since_days = max(1, min(int(since_days), 365))
    with _connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   COUNT(DISTINCT sub) AS users
            FROM tool_call_log
            WHERE called_at >= NOW() - make_interval(days => %s)
            """,
            (since_days,),
        ).fetchone() or {}
        by_tool = conn.execute(
            """
            SELECT tool_name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   ROUND(AVG(duration_ms))::int AS avg_ms
            FROM tool_call_log
            WHERE called_at >= NOW() - make_interval(days => %s)
            GROUP BY tool_name
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        by_user = conn.execute(
            """
            SELECT l.sub, u.email, u.name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT l.ok) AS errors
            FROM tool_call_log l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE l.called_at >= NOW() - make_interval(days => %s)
            GROUP BY l.sub, u.email, u.name
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        by_day = conn.execute(
            """
            SELECT to_char(called_at::date, 'YYYY-MM-DD') AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors
            FROM tool_call_log
            WHERE called_at >= NOW() - make_interval(days => %s)
            GROUP BY called_at::date
            ORDER BY called_at::date
            """,
            (since_days,),
        ).fetchall()
    return {
        "since_days": since_days,
        "total_calls": int((totals or {}).get("total") or 0),
        "error_count": int((totals or {}).get("errors") or 0),
        "active_users": int((totals or {}).get("users") or 0),
        "by_tool": list(by_tool),
        "by_user": list(by_user),
        "by_day": list(by_day),
    }


def prune_tool_call_log(keep_days: int = 30) -> int:
    """Retire les lignes de journal plus vieilles que `keep_days`. Borne la
    volumétrie (appelé au boot dans init_db). Retourne le nombre de lignes
    supprimées."""
    keep_days = max(1, int(keep_days))
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM tool_call_log WHERE called_at < NOW() - make_interval(days => %s)",
            (keep_days,),
        )
        return cur.rowcount or 0


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


def replace_user_disabled_tools(sub: str, tool_names: list[str]) -> None:
    """Remplace l'ensemble des disabled_tools du user par celui passé.

    Utilisé par `apply_user_preset` pour basculer en un appel atomique.
    """
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_disabled_tools WHERE sub = %s", (sub,))
            if tool_names:
                conn.executemany(
                    "INSERT INTO user_disabled_tools (sub, tool_name) VALUES (%s, %s)",
                    [(sub, t) for t in tool_names],
                )


# --- per-user enabled overrides (pour les tools masqués par défaut) ---------


def list_user_enabled_tools(sub: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool_name FROM user_enabled_tools WHERE sub = %s ORDER BY tool_name",
            (sub,),
        ).fetchall()
        return [r["tool_name"] for r in rows]


def add_user_enabled_tool(sub: str, tool_name: str) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_enabled_tools (sub, tool_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (sub, tool_name),
        )


def remove_user_enabled_tool(sub: str, tool_name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_enabled_tools WHERE sub = %s AND tool_name = %s",
            (sub, tool_name),
        )


def replace_user_enabled_tools(sub: str, tool_names: list[str]) -> None:
    """Remplace l'ensemble des enabled-overrides du user (bascule preset)."""
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_enabled_tools WHERE sub = %s", (sub,))
            if tool_names:
                conn.executemany(
                    "INSERT INTO user_enabled_tools (sub, tool_name) VALUES (%s, %s)",
                    [(sub, t) for t in tool_names],
                )


# --- per-user presets -------------------------------------------------------

def list_user_presets(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, enabled_tools, updated_at FROM user_presets "
            "WHERE sub = %s ORDER BY name",
            (sub,),
        ).fetchall()
        return [
            {
                "name": r["name"],
                "enabled_tools": list(r["enabled_tools"] or []),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]


def get_user_preset(sub: str, name: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, enabled_tools, updated_at FROM user_presets "
            "WHERE sub = %s AND name = %s",
            (sub, name),
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "enabled_tools": list(row["enabled_tools"] or []),
            "updated_at": row["updated_at"],
        }


def save_user_preset(sub: str, name: str, enabled_tools: list[str]) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_presets (sub, name, enabled_tools) VALUES (%s, %s, %s) "
            "ON CONFLICT (sub, name) DO UPDATE SET "
            "enabled_tools = EXCLUDED.enabled_tools, updated_at = NOW()",
            (sub, name, enabled_tools),
        )


def delete_user_preset(sub: str, name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_presets WHERE sub = %s AND name = %s",
            (sub, name),
        )
        return (cur.rowcount or 0) > 0


def get_usage_today(sub: str, tool: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE sub = %s AND tool = %s AND day = CURRENT_DATE",
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0


# --- platform keys (admin-managed) ------------------------------------------
#
# Chiffrement au repos (obligatoire) : miroir EXACT du pattern
# connector_credentials (cf. credentials_store). `api_key_enc` porte l'enveloppe
# AES-256-GCM ; pas de colonne plaintext. AAD = (provider, label) — stable sur
# l'UNIQUE(provider, label), anti-transplant.

def _pk_aad(provider: str, label: str) -> str:
    return f"platform_keys:{provider}:{label}"


def _pk_encrypt(provider: str, label: str, api_key: str) -> str:
    """Enveloppe AES-256-GCM à écrire. crypto.encrypt lève si master key absente
    (pas de stockage plaintext)."""
    from . import crypto
    return crypto.encrypt(api_key, _pk_aad(provider, label))


def _pk_reveal(row: dict, provider: str) -> Optional[str]:
    """api_key en clair depuis une ligne platform_keys : déchiffre `api_key_enc`.
    Chiffrement obligatoire (pas de plaintext) → un échec LÈVE, jamais de
    fallback silencieux."""
    enc = row.get("api_key_enc")
    if not enc:
        return None
    from . import crypto
    return crypto.decrypt(enc, _pk_aad(provider, row["label"]))


def list_platform_keys(provider: Optional[str] = None) -> list[dict]:
    """Liste les platform keys. **Inclut `api_key`** (déchiffré) — réservé à
    l'admin backend, jamais retourné via /api (la route admin masque ce champ).
    """
    sql = "SELECT id, provider, label, api_key_enc, created_at FROM platform_keys"
    params: tuple = ()
    if provider:
        sql += " WHERE provider = %s"
        params = (provider,)
    sql += " ORDER BY provider, created_at"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["api_key"] = _pk_reveal(d, d["provider"])
        d.pop("api_key_enc", None)
        out.append(d)
    return out


def get_platform_key(key_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, provider, label, api_key_enc, created_at "
            "FROM platform_keys WHERE id = %s",
            (key_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = _pk_reveal(d, d["provider"])
    d.pop("api_key_enc", None)
    return d


def create_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée une platform key. Renvoie l'id ; lève ValueError sur (provider, label) duplicata."""
    _check_provider(provider)
    if not label or not api_key:
        raise ValueError("label et api_key requis")
    enc = _pk_encrypt(provider, label, api_key)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO platform_keys (provider, label, api_key_enc) "
                "VALUES (%s, %s, %s) RETURNING id",
                (provider, label, enc),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"({provider}, {label}) existe déjà") from e
        return int(row["id"])


def upsert_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée ou met à jour la clé pour (provider, label). Idempotent — utilisé
    par le bootstrap des env vars au démarrage.
    """
    _check_provider(provider)
    enc = _pk_encrypt(provider, label, api_key)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO platform_keys (provider, label, api_key_enc)
            VALUES (%s, %s, %s)
            ON CONFLICT(provider, label) DO UPDATE SET
                api_key_enc = EXCLUDED.api_key_enc
            RETURNING id
            """,
            (provider, label, enc),
        ).fetchone()
        return int(row["id"])


def delete_platform_key(key_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM platform_keys WHERE id = %s", (key_id,))


# --- grants -----------------------------------------------------------------

def grant_platform_key(
    sub: str,
    platform_key_id: int,
    granted_by: Optional[str] = None,
    daily_quota: Optional[int] = None,
) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_grants (sub, platform_key_id, granted_at, granted_by, daily_quota)
            VALUES (%s, %s, NOW(), %s, %s)
            ON CONFLICT(sub, platform_key_id) DO UPDATE SET
                granted_at = NOW(),
                granted_by = EXCLUDED.granted_by,
                daily_quota = EXCLUDED.daily_quota
            """,
            (sub, platform_key_id, granted_by, daily_quota),
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
            SELECT pk.id AS platform_key_id, pk.provider, pk.label,
                   ug.granted_at, ug.granted_by, ug.daily_quota
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
            SELECT pk.id AS platform_key_id, pk.label, pk.api_key_enc,
                   ug.daily_quota
              FROM user_grants ug
              JOIN platform_keys pk ON pk.id = ug.platform_key_id
             WHERE ug.sub = %s AND pk.provider = %s
             ORDER BY ug.granted_at DESC
             LIMIT 1
            """,
            (sub, provider),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = _pk_reveal(d, provider)   # déchiffre JIT (resolve_api_key)
    d.pop("api_key_enc", None)
    return d


def list_users_with_grants() -> list[dict]:
    """Pour /api/admin/users — chaque user + ses grants (sans api_key)."""
    users = list_users()
    out = []
    for u in users:
        u = dict(u)
        u["grants"] = list_grants_for_user(u["sub"])
        out.append(u)
    return out


# --- namespace grants (deny-by-default pour namespaces sensibles) -----------

def grant_namespace(sub: str, namespace: str, granted_by: Optional[str] = None) -> None:
    """Accorde à `sub` l'accès au namespace sensible `namespace` (idempotent)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_namespace_grants (sub, namespace, granted_by)
            VALUES (%s, %s, %s)
            ON CONFLICT(sub, namespace) DO UPDATE SET
                granted_at = NOW(),
                granted_by = EXCLUDED.granted_by
            """,
            (sub, namespace, granted_by),
        )


def revoke_namespace(sub: str, namespace: str) -> bool:
    """Révoque l'accès. Renvoie True si un grant existait."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_namespace_grants WHERE sub = %s AND namespace = %s",
            (sub, namespace),
        )
        return (cur.rowcount or 0) > 0


def list_user_granted_namespaces(sub: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace FROM user_namespace_grants WHERE sub = %s ORDER BY namespace",
            (sub,),
        ).fetchall()
        return [r["namespace"] for r in rows]


def list_namespace_grants(namespace: Optional[str] = None) -> list[dict]:
    """Tous les grants de namespace (vue admin), filtrable par namespace."""
    sql = "SELECT sub, namespace, granted_by, granted_at FROM user_namespace_grants"
    params: tuple = ()
    if namespace:
        sql += " WHERE namespace = %s"
        params = (namespace,)
    sql += " ORDER BY namespace, granted_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# --- Google OAuth -----------------------------------------------------------

GOOGLE = "google"   # connecteur Google dans le coffre (account = email)


def _google_row(account: str, cur: dict) -> dict:
    """Reconstruit le dict legacy (contrat google_oauth.py) depuis une ligne coffre
    (cur = {secret, meta, set_at})."""
    m = cur["meta"]
    return {
        "google_email": account or None,
        "refresh_token": cur["secret"],
        "access_token": m.get("access_token"),
        "expires_at": m.get("expires_at"),
        "scopes": m.get("scopes"),
        "is_default": bool(m.get("is_default")),
        "granted_at": m.get("granted_at"),
        "updated_at": cur["set_at"],
    }


def set_google_oauth(
    sub: str,
    google_email: str,
    refresh_token: str,
    scopes: str,
    access_token: Optional[str] = None,
    expires_at: Optional[str] = None,
    make_default: Optional[bool] = None,
) -> None:
    """Upsert un compte Google dans le COFFRE (connector='google', account=email ;
    satellites — access_token/expires_at/scopes/is_default/granted_at — dans meta).

    `make_default` None → défaut si 1er compte. is_default conservé si déjà défaut
    (existing OR new). Claime la ligne mono pré-migration (account='').
    """
    upsert_user(sub)
    from . import credentials_store
    account = google_email or ""
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    n_named = sum(1 for a in accts if a["account"])
    prior = next((a for a in accts if a["account"] == account), None)
    if make_default is None:
        make_default = n_named == 0
    is_default = bool(prior and prior["meta"].get("is_default")) or make_default
    granted_at = (prior["meta"].get("granted_at") if prior else None) \
        or datetime.now(timezone.utc).isoformat()
    meta = {"access_token": access_token, "expires_at": expires_at, "scopes": scopes,
            "is_default": is_default, "granted_at": granted_at}
    with _connect() as conn:
        with conn.transaction():
            if account:   # claim l'éventuelle ligne mono pré-migration (account='')
                credentials_store.clear_credential("user", sub, GOOGLE, account="", conn=conn)
            if make_default:   # un seul défaut : retire le flag aux autres comptes
                conn.execute(
                    "UPDATE connector_credentials SET meta = jsonb_set(meta, '{is_default}', 'false') "
                    "WHERE entity_type='user' AND entity_id=%s AND connector=%s AND account<>%s",
                    (sub, GOOGLE, account),
                )
            credentials_store.set_credential(
                "user", sub, GOOGLE, refresh_token, set_by=sub,
                meta=meta, account=account, conn=conn)


def update_google_access_token(
    sub: str, google_email: Optional[str], access_token: str, expires_at: str
) -> None:
    """Met à jour SEULEMENT l'access_token + expiry (sur refresh) — merge meta dans
    le coffre, SANS re-chiffrer le refresh_token. `google_email` None = compte mono
    (account='')."""
    from . import credentials_store
    account = google_email or ""
    credentials_store.update_meta(
        "user", sub, GOOGLE, account,
        {"access_token": access_token, "expires_at": expires_at})


def get_google_oauth(sub: str, account: Optional[str] = None) -> Optional[dict]:
    """Renvoie un compte Google du user depuis le COFFRE (déchiffre le
    refresh_token). `account` (email) cible un compte ; None = le défaut
    (meta.is_default), à défaut le plus ancien (granted_at)."""
    from . import credentials_store
    if account:
        cur = credentials_store.get_credential_with_meta("user", sub, GOOGLE, account=account)
        return _google_row(account, cur) if cur else None
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    if not accts:
        return None
    chosen = next((a for a in accts if a["meta"].get("is_default")), None) \
        or min(accts, key=lambda a: a["meta"].get("granted_at") or "")
    cur = credentials_store.get_credential_with_meta("user", sub, GOOGLE, account=chosen["account"])
    return _google_row(chosen["account"], cur) if cur else None


def list_google_accounts(sub: str) -> list[dict]:
    """Liste les comptes Google connectés (sans les tokens) — depuis le coffre."""
    from . import credentials_store
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    out = [{
        "google_email": a["account"] or None,
        "is_default": bool(a["meta"].get("is_default")),
        "scopes": a["meta"].get("scopes"),
        "granted_at": a["meta"].get("granted_at"),
        "updated_at": a["set_at"],
    } for a in accts]
    out.sort(key=lambda r: (not r["is_default"], r["granted_at"] or ""))
    return out


def set_default_google_account(sub: str, account: str) -> bool:
    """Marque `account` comme défaut (meta.is_default) dans le coffre. False si le
    compte n'existe pas."""
    from . import credentials_store
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    if not any(a["account"] == account for a in accts):
        return False
    with _connect() as conn:
        conn.execute(
            "UPDATE connector_credentials "
            "SET meta = jsonb_set(meta, '{is_default}', to_jsonb(account = %s)) "
            "WHERE entity_type='user' AND entity_id=%s AND connector=%s",
            (account, sub, GOOGLE),
        )
    return True


def delete_google_oauth(sub: str, account: Optional[str] = None) -> None:
    """Supprime un compte (account=email) ou tous (account=None) du coffre. Si on
    retire le défaut et qu'il reste des comptes, promeut le plus ancien."""
    from . import credentials_store
    with _connect() as conn:
        with conn.transaction():
            if account is None:
                conn.execute(
                    "DELETE FROM connector_credentials "
                    "WHERE entity_type='user' AND entity_id=%s AND connector=%s", (sub, GOOGLE))
                return
            credentials_store.clear_credential("user", sub, GOOGLE, account=account, conn=conn)
            # promotion du défaut : lire le RESTANT dans CETTE transaction (voit le delete)
            rem = conn.execute(
                "SELECT account, meta FROM connector_credentials "
                "WHERE entity_type='user' AND entity_id=%s AND connector=%s", (sub, GOOGLE)).fetchall()
            if rem and not any((r["meta"] or {}).get("is_default") for r in rem):
                oldest = min(rem, key=lambda r: (r["meta"] or {}).get("granted_at") or "")["account"]
                conn.execute(
                    "UPDATE connector_credentials SET meta = jsonb_set(meta, '{is_default}', 'true') "
                    "WHERE entity_type='user' AND entity_id=%s AND connector=%s AND account=%s",
                    (sub, GOOGLE, oldest))


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


# --- Datastore shares --------------------------------------------------------

def share_datastore_namespace(
    owner_sub: str, namespace: str, shared_with_sub: str, permission: str = "write",
) -> int:
    ns = get_datastore_namespace(owner_sub, namespace)
    if not ns:
        raise ValueError(f"namespace `{namespace}` not found for owner")
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO datastore_shares (owner_sub, namespace, spreadsheet_id, shared_with_sub, permission) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (owner_sub, namespace, ns["spreadsheet_id"], shared_with_sub, permission),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            conn.execute(
                "UPDATE datastore_shares SET permission = %s, spreadsheet_id = %s "
                "WHERE owner_sub = %s AND namespace = %s AND shared_with_sub = %s",
                (permission, ns["spreadsheet_id"], owner_sub, namespace, shared_with_sub),
            )
            return 0
        return int(row["id"])


def unshare_datastore_namespace(owner_sub: str, namespace: str, shared_with_sub: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM datastore_shares WHERE owner_sub = %s AND namespace = %s AND shared_with_sub = %s",
            (owner_sub, namespace, shared_with_sub),
        )
        return cur.rowcount > 0


def get_shared_namespace(sub: str, namespace: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM datastore_shares WHERE shared_with_sub = %s AND namespace = %s LIMIT 1",
            (sub, namespace),
        ).fetchone()
        return dict(row) if row else None


def list_shared_namespaces(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace, spreadsheet_id, owner_sub, permission, created_at "
            "FROM datastore_shares WHERE shared_with_sub = %s ORDER BY namespace",
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


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
