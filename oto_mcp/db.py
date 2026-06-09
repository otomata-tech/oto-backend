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
    kaspr_api_key TEXT,
    pennylane_api_key TEXT,
    slack_api_key TEXT,
    fullenrich_api_key TEXT,
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
    api_key TEXT,                          -- plaintext (soak/legacy) ; NULL une fois chiffré
    api_key_enc TEXT,                      -- enveloppe AES-256-GCM (Phase 7 folding)
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

CREATE TABLE IF NOT EXISTS user_google_oauth (
    sub TEXT NOT NULL,
    google_email TEXT,
    refresh_token TEXT NOT NULL,
    access_token TEXT,
    expires_at TIMESTAMPTZ,
    scopes TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT TRUE,
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
-- secrets propres (org_secrets), un set de namespaces autorisés
-- (org_entitlements), et des opérateurs (org_members). Source de vérité de
-- l'appartenance = ces tables, résolues par `sub` — JAMAIS un claim du token
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

-- Credential de compte POSSÉDÉ par l'org et partagé à ses membres (Attio,
-- Pennylane, MM token, clé API stateless…). provider validé comme org-partageable
-- (byo_org) via le registre : inclut mm (org-only, non-keyed), exclut
-- slack/linkedin/google (sessions physiologiquement personnelles).
CREATE TABLE IF NOT EXISTS org_secrets (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    api_key TEXT NOT NULL,
    set_by TEXT,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, provider)
);

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

-- Credentials génériques per-entité (user OU org) — table unique qui remplace
-- les 9 colonnes users.<provider>_api_key + org_secrets. entity_id = sub (user)
-- ou org_id::text (org) ; toujours requêter (entity_type, entity_id) ENSEMBLE.
-- `secret` en clair pour l'instant ; chiffré par enveloppe en Phase 7 (le
-- déchiffrement JIT vivra dans resolve_api_key). meta JSONB pour les satellites
-- futurs (user_agent, scopes…). Source canonique des credentials keyed.
CREATE TABLE IF NOT EXISTS connector_credentials (
    entity_type TEXT NOT NULL,            -- 'user' | 'org'
    entity_id   TEXT NOT NULL,            -- users.sub | orgs.id::text
    connector   TEXT NOT NULL,            -- nom de connecteur (registre)
    account     TEXT NOT NULL DEFAULT '', -- discriminant multi-compte ('' = mono ; ex. email Google)
    secret      TEXT,                     -- clair (chiffrement off, ou soak)
    secret_enc  TEXT,                     -- enveloppe AES-256-GCM (Phase 7)
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
        # Idempotent column adds — `CREATE TABLE IF NOT EXISTS` ne propage pas
        # les nouvelles colonnes sur les tables existantes. Ajouter ici à chaque
        # nouveau provider key.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS kaspr_api_key TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pennylane_api_key TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS slack_api_key TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS fullenrich_api_key TEXT")
        conn.execute("ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS daily_quota INTEGER")
        # Google OAuth : passage mono-compte → multi-compte par user.
        # L'ancienne table avait `sub` en PRIMARY KEY (1 compte). On ajoute
        # google_email + is_default, on droppe la PK sur sub, et on unicise
        # par (sub, google_email). Les lignes existantes ont google_email NULL
        # (compte « legacy ») : elles restent servies comme défaut et sont
        # claimées proprement au prochain consentement (cf. set_google_oauth).
        conn.execute("ALTER TABLE user_google_oauth ADD COLUMN IF NOT EXISTS google_email TEXT")
        conn.execute("ALTER TABLE user_google_oauth ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("ALTER TABLE user_google_oauth DROP CONSTRAINT IF EXISTS user_google_oauth_pkey")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS user_google_oauth_sub_email "
            "ON user_google_oauth(sub, google_email)"
        )
        # Phase 7 : colonne chiffrée (idempotent — couvre le cas où la table a été
        # créée par la Phase 2 avant ce déploiement).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS secret_enc TEXT")
        # Multi-compte : discriminant `account` dans la PK (folding session secrets).
        # Idempotent ; account='' pour les lignes existantes (mono-compte) → la PG
        # à 4 colonnes reste unique. DROP+ADD PK chaque boot (table sans FK entrante).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE connector_credentials DROP CONSTRAINT IF EXISTS connector_credentials_pkey")
        conn.execute("ALTER TABLE connector_credentials ADD PRIMARY KEY (entity_type, entity_id, connector, account)")
        _backfill_connector_credentials(conn)
        _backfill_session_secrets(conn)   # folding linkedin/crunchbase → coffre
        # platform_keys : colonne chiffrée + api_key nullable (Phase 7 folding,
        # idempotent — couvre les tables créées avant ce déploiement).
        conn.execute("ALTER TABLE platform_keys ADD COLUMN IF NOT EXISTS api_key_enc TEXT")
        conn.execute("ALTER TABLE platform_keys ALTER COLUMN api_key DROP NOT NULL")
        # Migration de chiffrement (no-op si OTO_MCP_MASTER_KEY absente) : chiffre
        # en place les secrets encore en clair, dans la même transaction.
        from . import credentials_store
        credentials_store.encrypt_existing_rows(conn)
        _encrypt_existing_platform_keys(conn)
        _drop_plaintext_after_soak(conn)


def _drop_plaintext_after_soak(conn: psycopg.Connection) -> None:
    """Runbook FINAL du chiffrement-at-rest (Phase 7) : retire les 4 emplacements
    plaintext résiduels. DÉLIBÉRÉ — ne tourne QUE si chiffrement activé ET
    `OTO_MCP_CRYPTO_DROP_PLAINTEXT=1` (à poser une fois le déchiffrement éprouvé
    en prod). Sinon no-op (soak : on garde le plaintext en filet).

    Ordre : self-check + null de connector_credentials.secret (lève si une ligne
    chiffrée ne déchiffre pas → abort), puis les 9 colonnes users.<provider>_api_key,
    puis org_secrets (DELETE), puis platform_keys.api_key (self-check decrypt +
    null). Idempotent. Dans la transaction d'init_db."""
    from . import credentials_store, crypto
    if not (crypto.encryption_enabled() and os.environ.get("OTO_MCP_CRYPTO_DROP_PLAINTEXT") == "1"):
        return
    credentials_store.verify_and_null_plaintext(conn)
    for provider in KEY_PROVIDERS:
        col = f"{provider}_api_key"
        conn.execute(f"UPDATE users SET {col} = NULL WHERE {col} IS NOT NULL")
    conn.execute("DELETE FROM org_secrets")
    # platform_keys : self-check decrypt de chaque ligne chiffrée AVANT de nuller
    # (lève → abort/rollback plutôt que perdre le plaintext), puis null.
    pk_rows = conn.execute(
        "SELECT provider, label, api_key_enc FROM platform_keys "
        "WHERE api_key IS NOT NULL AND api_key_enc IS NOT NULL"
    ).fetchall()
    for r in pk_rows:
        crypto.decrypt(r["api_key_enc"], _pk_aad(r["provider"], r["label"]))
    conn.execute("UPDATE platform_keys SET api_key = NULL WHERE api_key_enc IS NOT NULL")
    # Secrets de session foldés (linkedin/crunchbase) : le coffre est canonique
    # et chiffré ; on nulle les colonnes legacy résiduelles sur users.
    conn.execute(
        "UPDATE users SET linkedin_cookie = NULL, linkedin_user_agent = NULL, "
        "crunchbase_cookies = NULL, crunchbase_user_agent = NULL "
        "WHERE linkedin_cookie IS NOT NULL OR crunchbase_cookies IS NOT NULL"
    )
    # Google foldé → coffre chiffré canonique : on vide la table legacy.
    conn.execute("DELETE FROM user_google_oauth")


def _backfill_connector_credentials(conn: psycopg.Connection) -> None:
    """Recopie idempotente des credentials legacy → connector_credentials
    (Phase 2/C2). Rejoué à chaque boot, no-op après le 1er via ON CONFLICT DO
    NOTHING : ne remplit que les lignes manquantes, n'écrase jamais (le
    dual-write garde connector_credentials à jour ensuite).

    - 9 colonnes users.<provider>_api_key (provider keyed) → entity 'user'.
    - org_secrets → entity 'org' (org_id::text).
    secret_kind dérivé du registre (slack=refresh_token, sinon api_key).
    """
    for provider in KEY_PROVIDERS:
        c = connectors.REGISTRY.get(provider)
        kind = c.secret_kind if c else "api_key"
        col = f"{provider}_api_key"
        conn.execute(
            f"""
            INSERT INTO connector_credentials (entity_type, entity_id, connector, secret, secret_kind)
            SELECT 'user', sub, %s, {col}, %s FROM users WHERE {col} IS NOT NULL
            ON CONFLICT (entity_type, entity_id, connector, account) DO NOTHING
            """,
            (provider, kind),
        )
    conn.execute(
        """
        INSERT INTO connector_credentials (entity_type, entity_id, connector, secret, secret_kind, set_by, set_at)
        SELECT 'org', org_id::text, provider, api_key, 'api_key', set_by, set_at FROM org_secrets
        ON CONFLICT (entity_type, entity_id, connector, account) DO NOTHING
        """
    )


def _backfill_session_secrets(conn: psycopg.Connection) -> None:
    """Recopie idempotente des secrets de session legacy → connector_credentials
    (folding LinkedIn/Crunchbase). Colonnes `users` → coffre ; UA dans meta ;
    set_at préservé. ON CONFLICT DO NOTHING (le dual-write garde le coffre à jour
    ensuite). Google (`user_google_oauth`, multi-compte) = barreau séparé."""
    conn.execute(
        """
        INSERT INTO connector_credentials
            (entity_type, entity_id, connector, secret, secret_kind, meta, set_at)
        SELECT 'user', sub, 'linkedin', linkedin_cookie, 'cookie',
               jsonb_build_object('user_agent', linkedin_user_agent),
               COALESCE(linkedin_cookie_set_at, NOW())
        FROM users WHERE linkedin_cookie IS NOT NULL
        ON CONFLICT (entity_type, entity_id, connector, account) DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO connector_credentials
            (entity_type, entity_id, connector, secret, secret_kind, meta, set_at)
        SELECT 'user', sub, 'crunchbase', crunchbase_cookies, 'cookie',
               jsonb_build_object('user_agent', crunchbase_user_agent),
               COALESCE(crunchbase_set_at, NOW())
        FROM users WHERE crunchbase_cookies IS NOT NULL
        ON CONFLICT (entity_type, entity_id, connector, account) DO NOTHING
        """
    )
    # Google : multi-compte → account = email (COALESCE '' pour la ligne mono
    # legacy NULL). Satellites dans meta. isoformat 'T' (cohérent avec le code).
    conn.execute(
        """
        INSERT INTO connector_credentials
            (entity_type, entity_id, connector, account, secret, secret_kind, meta, set_at)
        SELECT 'user', sub, 'google', COALESCE(google_email, ''), refresh_token, 'oauth',
               jsonb_build_object(
                   'access_token', access_token, 'expires_at', expires_at,
                   'scopes', scopes, 'is_default', is_default,
                   'granted_at', to_char(granted_at, 'YYYY-MM-DD"T"HH24:MI:SS+00:00')),
               updated_at
        FROM user_google_oauth WHERE refresh_token IS NOT NULL
        ON CONFLICT (entity_type, entity_id, connector, account) DO NOTHING
        """
    )


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

    Coffre unique (folding) : secret = cookie, UA dans meta. Dual-write ATOMIQUE
    legacy `users` + connector_credentials (conditionnel au chiffrement, comme
    set_user_api_key). UA effectif résolu depuis le coffre si non fourni
    (équivalent du COALESCE legacy, robuste même colonnes legacy nullées)."""
    upsert_user(sub)
    from . import credentials_store, crypto
    ua = user_agent
    if ua is None:
        cur = credentials_store.get_credential_with_meta("user", sub, "linkedin")
        ua = cur["meta"].get("user_agent") if cur else None
    with _connect() as conn:
        with conn.transaction():
            if not crypto.encryption_enabled():
                conn.execute(
                    "UPDATE users SET linkedin_cookie = %s, linkedin_user_agent = %s, "
                    "linkedin_cookie_set_at = NOW(), updated_at = NOW() WHERE sub = %s",
                    (cookie, ua, sub),
                )
            credentials_store.set_credential(
                "user", sub, "linkedin", cookie, set_by=sub,
                meta={"user_agent": ua}, conn=conn)


def clear_linkedin_cookie(sub: str) -> None:
    from . import credentials_store
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE users SET linkedin_cookie = NULL, linkedin_user_agent = NULL, "
                "linkedin_cookie_set_at = NULL, updated_at = NOW() WHERE sub = %s",
                (sub,),
            )
            credentials_store.clear_credential("user", sub, "linkedin", conn=conn)


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
    """Store cookies (JSON-encoded list) + UA. Coffre unique (folding) : secret =
    cookies_json, UA dans meta. Dual-write ATOMIQUE legacy + coffre (cf.
    set_linkedin_cookie)."""
    upsert_user(sub)
    from . import credentials_store, crypto
    ua = user_agent
    if ua is None:
        cur = credentials_store.get_credential_with_meta("user", sub, "crunchbase")
        ua = cur["meta"].get("user_agent") if cur else None
    with _connect() as conn:
        with conn.transaction():
            if not crypto.encryption_enabled():
                conn.execute(
                    "UPDATE users SET crunchbase_cookies = %s, crunchbase_user_agent = %s, "
                    "crunchbase_set_at = NOW(), updated_at = NOW() WHERE sub = %s",
                    (cookies_json, ua, sub),
                )
            credentials_store.set_credential(
                "user", sub, "crunchbase", cookies_json, set_by=sub,
                meta={"user_agent": ua}, conn=conn)


def clear_crunchbase_session(sub: str) -> None:
    from . import credentials_store
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE users SET crunchbase_cookies = NULL, crunchbase_user_agent = NULL, "
                "crunchbase_set_at = NULL, updated_at = NOW() WHERE sub = %s",
                (sub,),
            )
            credentials_store.clear_credential("user", sub, "crunchbase", conn=conn)


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
    # Dual-write (Phase 2/C3) ATOMIQUE : colonne legacy + table canonique dans
    # UNE transaction (même conn), sinon un crash entre les deux divergerait les
    # stores (clé révoquée encore résolue / clé posée invisible). Import lazy
    # (db ne doit pas importer credentials_store au niveau module — cycle).
    from . import credentials_store, crypto
    col = f"{provider}_api_key"
    with _connect() as conn:
        with conn.transaction():
            # Chiffrement OFF : dual-write legacy (rollback Phase 2). Chiffrement
            # ON : on n'écrit PLUS de plaintext en legacy (canonique chiffré seul) ;
            # le legacy résiduel est nullé par le runbook de soak (_drop_plaintext).
            if not crypto.encryption_enabled():
                conn.execute(
                    f"UPDATE users SET {col} = %s, updated_at = NOW() WHERE sub = %s",
                    (key, sub),
                )
            credentials_store.set_credential("user", sub, provider, key, set_by=sub, conn=conn)


def clear_user_api_key(sub: str, provider: str) -> None:
    _check_provider(provider)
    from . import credentials_store
    col = f"{provider}_api_key"
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                f"UPDATE users SET {col} = NULL, updated_at = NOW() WHERE sub = %s",
                (sub,),
            )
            credentials_store.clear_credential("user", sub, provider, conn=conn)


def get_user_api_key(sub: str, provider: str) -> Optional[str]:
    # Cutover (Phase 2/C4) : lit la table canonique connector_credentials (et
    # non plus la colonne legacy users.<provider>_api_key, toujours dual-written
    # pour le rollback). Import lazy (anti-cycle). require_keyed dans le store.
    # Déchiffre si nécessaire (Phase 7) — chemin de RÉSOLUTION.
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
# Chiffrement au repos (Phase 7 folding) : miroir EXACT du pattern
# connector_credentials (cf. credentials_store). `api_key_enc` porte l'enveloppe
# AES-256-GCM ; `api_key` reste en clair tant que le chiffrement est OFF (pas de
# master key) OU pendant le soak (filet avant le null final). AAD = (provider,
# label) — stable sur l'UNIQUE(provider, label), anti-transplant. Inerte sans
# OTO_MCP_MASTER_KEY (encryption_enabled() == False → comportement identique).

def _pk_aad(provider: str, label: str) -> str:
    return f"platform_keys:{provider}:{label}"


def _pk_store(provider: str, label: str, api_key: str) -> tuple[Optional[str], Optional[str]]:
    """(plaintext, enveloppe) à écrire : chiffré → (None, ct) ; OFF → (api_key, None)."""
    from . import crypto
    if crypto.encryption_enabled():
        return None, crypto.encrypt(api_key, _pk_aad(provider, label))
    return api_key, None


def _pk_reveal(row: dict, provider: str) -> Optional[str]:
    """api_key en clair depuis une ligne platform_keys : déchiffre `api_key_enc`
    s'il est présent (fallback plaintext pendant le soak), sinon `api_key`."""
    enc = row.get("api_key_enc")
    if enc:
        from . import crypto
        try:
            return crypto.decrypt(enc, _pk_aad(provider, row["label"]))
        except Exception:
            if row.get("api_key") is not None:
                logger.warning(
                    "decrypt KO platform_key %s/%s — fallback plaintext (soak) ; "
                    "vérifier la master key", provider, row.get("label"),
                )
                return row["api_key"]
            raise
    return row.get("api_key")


def list_platform_keys(provider: Optional[str] = None) -> list[dict]:
    """Liste les platform keys. **Inclut `api_key`** (déchiffré) — réservé à
    l'admin backend, jamais retourné via /api (la route admin masque ce champ).
    """
    sql = "SELECT id, provider, label, api_key, api_key_enc, created_at FROM platform_keys"
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
            "SELECT id, provider, label, api_key, api_key_enc, created_at "
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
    plain, enc = _pk_store(provider, label, api_key)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO platform_keys (provider, label, api_key, api_key_enc) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (provider, label, plain, enc),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"({provider}, {label}) existe déjà") from e
        return int(row["id"])


def upsert_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée ou met à jour la clé pour (provider, label). Idempotent — utilisé
    par le bootstrap des env vars au démarrage.
    """
    _check_provider(provider)
    plain, enc = _pk_store(provider, label, api_key)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO platform_keys (provider, label, api_key, api_key_enc)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(provider, label) DO UPDATE SET
                api_key = EXCLUDED.api_key, api_key_enc = EXCLUDED.api_key_enc
            RETURNING id
            """,
            (provider, label, plain, enc),
        ).fetchone()
        return int(row["id"])


def delete_platform_key(key_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM platform_keys WHERE id = %s", (key_id,))


def _encrypt_existing_platform_keys(conn: psycopg.Connection) -> int:
    """Migration (Phase 7 folding) : chiffre en place les platform_keys encore en
    clair. Idempotent (WHERE api_key_enc IS NULL). No-op si chiffrement OFF. GARDE
    le plaintext (soak) — nullé par `_drop_plaintext_after_soak`. Miroir de
    credentials_store.encrypt_existing_rows. Dans la transaction d'init_db."""
    from . import crypto
    if not crypto.encryption_enabled():
        return 0
    rows = conn.execute(
        "SELECT id, provider, label, api_key FROM platform_keys "
        "WHERE api_key IS NOT NULL AND api_key_enc IS NULL"
    ).fetchall()
    for r in rows:
        enc = crypto.encrypt(r["api_key"], _pk_aad(r["provider"], r["label"]))
        conn.execute("UPDATE platform_keys SET api_key_enc = %s WHERE id = %s", (enc, r["id"]))
    return len(rows)


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
            SELECT pk.id AS platform_key_id, pk.label, pk.api_key, pk.api_key_enc,
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
    (existing OR new). Claime la ligne legacy mono (account='' coffre + google_email
    NULL legacy). Dual-write legacy ATOMIQUE conditionnel au chiffrement (rollback).
    """
    upsert_user(sub)
    from . import credentials_store, crypto
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
            if not crypto.encryption_enabled():   # mirror legacy (rollback)
                conn.execute(
                    "DELETE FROM user_google_oauth WHERE sub=%s AND google_email IS NULL", (sub,))
                if make_default:
                    conn.execute(
                        "UPDATE user_google_oauth SET is_default=FALSE WHERE sub=%s", (sub,))
                conn.execute(
                    """
                    INSERT INTO user_google_oauth
                        (sub, google_email, refresh_token, access_token, expires_at, scopes, is_default)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(sub, google_email) DO UPDATE SET
                        refresh_token = EXCLUDED.refresh_token,
                        access_token  = EXCLUDED.access_token,
                        expires_at    = EXCLUDED.expires_at,
                        scopes        = EXCLUDED.scopes,
                        is_default    = user_google_oauth.is_default OR EXCLUDED.is_default,
                        updated_at    = NOW()
                    """,
                    (sub, google_email, refresh_token, access_token, expires_at, scopes, make_default),
                )


def update_google_access_token(
    sub: str, google_email: Optional[str], access_token: str, expires_at: str
) -> None:
    """Met à jour SEULEMENT l'access_token + expiry (sur refresh) — merge meta dans
    le coffre, SANS re-chiffrer le refresh_token. `google_email` None = compte mono
    (account=''). Dual-write legacy conditionnel au chiffrement."""
    from . import credentials_store, crypto
    account = google_email or ""
    credentials_store.update_meta(
        "user", sub, GOOGLE, account,
        {"access_token": access_token, "expires_at": expires_at})
    if not crypto.encryption_enabled():
        with _connect() as conn:
            if google_email is None:
                conn.execute(
                    "UPDATE user_google_oauth SET access_token=%s, expires_at=%s, "
                    "updated_at=NOW() WHERE sub=%s AND google_email IS NULL",
                    (access_token, expires_at, sub))
            else:
                conn.execute(
                    "UPDATE user_google_oauth SET access_token=%s, expires_at=%s, "
                    "updated_at=NOW() WHERE sub=%s AND google_email=%s",
                    (access_token, expires_at, sub, google_email))


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
    compte n'existe pas. Dual-write legacy conditionnel au chiffrement."""
    from . import credentials_store, crypto
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    if not any(a["account"] == account for a in accts):
        return False
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE connector_credentials "
                "SET meta = jsonb_set(meta, '{is_default}', to_jsonb(account = %s)) "
                "WHERE entity_type='user' AND entity_id=%s AND connector=%s",
                (account, sub, GOOGLE),
            )
            if not crypto.encryption_enabled():
                conn.execute(
                    "UPDATE user_google_oauth SET is_default=(google_email=%s) WHERE sub=%s",
                    (account, sub))
    return True


def delete_google_oauth(sub: str, account: Optional[str] = None) -> None:
    """Supprime un compte (account=email) ou tous (account=None) du coffre. Si on
    retire le défaut et qu'il reste des comptes, promeut le plus ancien. Dual-write
    legacy conditionnel au chiffrement."""
    from . import credentials_store, crypto
    enc = crypto.encryption_enabled()
    with _connect() as conn:
        with conn.transaction():
            if account is None:
                conn.execute(
                    "DELETE FROM connector_credentials "
                    "WHERE entity_type='user' AND entity_id=%s AND connector=%s", (sub, GOOGLE))
                if not enc:
                    conn.execute("DELETE FROM user_google_oauth WHERE sub=%s", (sub,))
                return
            credentials_store.clear_credential("user", sub, GOOGLE, account=account, conn=conn)
            if not enc:
                conn.execute(
                    "DELETE FROM user_google_oauth WHERE sub=%s AND google_email=%s", (sub, account))
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
                if not enc:
                    conn.execute(
                        "UPDATE user_google_oauth SET is_default=TRUE WHERE sub=%s AND google_email=%s",
                        (sub, oldest))


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
