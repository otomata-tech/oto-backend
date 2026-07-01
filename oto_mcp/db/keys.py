"""Clés API user + clés plateforme + grants (user/org) + RBAC connecteur + schémas de connecteur observés.

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

from ._conn import KEY_PROVIDERS, _connect
from .users import list_users, upsert_user


def _check_provider(provider: str) -> None:
    if provider not in KEY_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (allowed: {KEY_PROVIDERS})")


# Scope MEMBRE (ADR 0033) : la clé per-user est keyée (sub, org) — « ma clé dans
# CETTE org ». L'org est TOUJOURS passée par l'appelant (access/api_routes résolvent
# le contexte via le seam `current_org` ; la couche db ne le lit jamais elle-même).
# `org_id=None` (défensif, contexte org introuvable) → pas de clé, jamais un repli
# org-agnostique : c'était le trou que 0033 ferme.

def set_member_api_key(sub: str, org_id: int, provider: str, key: str) -> None:
    _check_provider(provider)
    upsert_user(sub)
    # Coffre chiffré, source unique. Import lazy (db ne doit pas importer
    # credentials_store au niveau module — cycle).
    from .. import credentials_store
    credentials_store.set_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub),
        provider, key, set_by=sub)


def clear_member_api_key(sub: str, org_id: int, provider: str) -> None:
    _check_provider(provider)
    from .. import credentials_store
    credentials_store.clear_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub), provider)


def get_member_api_key(sub: str, org_id: Optional[int], provider: str) -> Optional[str]:
    # Lit le coffre `connector_credentials` (déchiffre — chemin de RÉSOLUTION).
    # Import lazy (anti-cycle) ; require_keyed dans le store.
    if org_id is None:
        return None
    from .. import credentials_store
    return credentials_store.get_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub), provider)


def has_member_api_key(sub: str, org_id: Optional[int], provider: str) -> bool:
    """Présence de la clé du membre dans CETTE org, SANS déchiffrer (status_for)."""
    if org_id is None:
        return False
    from .. import credentials_store
    return credentials_store.has_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub), provider)


def _pk_aad(provider: str, label: str) -> str:
    return f"platform_keys:{provider}:{label}"


def _pk_encrypt(provider: str, label: str, api_key: str) -> str:
    """Enveloppe AES-256-GCM à écrire. crypto.encrypt lève si master key absente
    (pas de stockage plaintext)."""
    from .. import crypto
    return crypto.encrypt(api_key, _pk_aad(provider, label))


def _pk_reveal(row: dict, provider: str) -> Optional[str]:
    """api_key en clair depuis une ligne platform_keys : déchiffre `api_key_enc`.
    Chiffrement obligatoire (pas de plaintext) → un échec LÈVE, jamais de
    fallback silencieux."""
    enc = row.get("api_key_enc")
    if not enc:
        return None
    from .. import crypto
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


def get_platform_api_key(provider: str) -> Optional[dict]:
    """Clé plateforme la plus récente d'un provider, déchiffrée (free-tier ADR 0031).
    Renvoie {api_key, label} ou None — utilisée SANS grant pour les connecteurs
    `platform_key_open` (quota gratuit per-user appliqué dans resolve_credential)."""
    keys = list_platform_keys(provider)  # ORDER BY created_at ASC → dernière = la + récente
    if not keys:
        return None
    k = keys[-1]
    return {"api_key": k["api_key"], "label": k["label"]}


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


def grant_org_platform_key(org_id: int, platform_key_id: int,
                           granted_by: Optional[str] = None,
                           daily_quota: Optional[int] = None) -> None:
    """Partage une clé plateforme à TOUTE l'org (couche 2). Miroir org de grant_platform_key."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_grants (org_id, platform_key_id, granted_at, granted_by, daily_quota)
            VALUES (%s, %s, NOW(), %s, %s)
            ON CONFLICT(org_id, platform_key_id) DO UPDATE SET
                granted_at = NOW(), granted_by = EXCLUDED.granted_by,
                daily_quota = EXCLUDED.daily_quota
            """,
            (org_id, platform_key_id, granted_by, daily_quota),
        )


def revoke_org_platform_key(org_id: int, platform_key_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM org_grants WHERE org_id = %s AND platform_key_id = %s",
                     (org_id, platform_key_id))


def list_org_grants(org_id: int) -> list[dict]:
    """Grants de clé plateforme d'une org (joint platform_keys, sans api_key brut)."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.provider, pk.label,
                   og.granted_at, og.granted_by, og.daily_quota
              FROM org_grants og JOIN platform_keys pk ON pk.id = og.platform_key_id
             WHERE og.org_id = %s ORDER BY pk.provider, og.granted_at DESC
            """,
            (org_id,),
        )]


def get_active_org_grant(org_id: int, provider: str) -> Optional[dict]:
    """Grant de clé plateforme de l'org pour `provider` (le plus récent), ou None.
    Miroir org de get_active_grant — résout la clé plateforme partagée à l'org."""
    _check_provider(provider)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.label, pk.api_key_enc, og.daily_quota
              FROM org_grants og JOIN platform_keys pk ON pk.id = og.platform_key_id
             WHERE og.org_id = %s AND pk.provider = %s
             ORDER BY og.granted_at DESC LIMIT 1
            """,
            (org_id, provider),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = _pk_reveal(d, provider)
    d.pop("api_key_enc", None)
    return d


# ── RBAC connecteur interne à l'org (ADR 0025) ──────────────────────────────
def set_connector_access(org_id: int, connector: str, principal_type: str,
                         principal_id: str, granted_by: Optional[str] = None) -> None:
    """Autorise un principal (groupe/user) sur un connecteur dans l'org → le rend
    RESTREINT (deny-by-default) s'il ne l'était pas. Idempotent."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_connector_access (org_id, connector, principal_type, principal_id, granted_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (org_id, connector, principal_type, principal_id) DO NOTHING
            """,
            (org_id, connector, principal_type, str(principal_id), granted_by),
        )


def clear_connector_access(org_id: int, connector: str, principal_type: str,
                           principal_id: str) -> None:
    """Retire un principal. Quand la dernière ligne d'un (org, connector) part,
    le connecteur redevient OUVERT à toute l'org (absence ⟹ non restreint)."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM org_connector_access WHERE org_id = %s AND connector = %s "
            "AND principal_type = %s AND principal_id = %s",
            (org_id, connector, principal_type, str(principal_id)),
        )


def list_connector_access(org_id: int, connector: Optional[str] = None) -> list[dict]:
    """ACL connecteur de l'org : [{connector, principal_type, principal_id, granted_at}]."""
    sql = ("SELECT connector, principal_type, principal_id, granted_by, granted_at "
           "FROM org_connector_access WHERE org_id = %s")
    args: tuple = (org_id,)
    if connector is not None:
        sql += " AND connector = %s"
        args = (org_id, connector)
    sql += " ORDER BY connector, principal_type, principal_id"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def org_restricted_connectors(org_id: int) -> set:
    """Connecteurs RESTREINTS dans l'org (≥1 ligne d'ACL) — deny-by-default pour eux."""
    with _connect() as conn:
        return {r["connector"] for r in conn.execute(
            "SELECT DISTINCT connector FROM org_connector_access WHERE org_id = %s",
            (org_id,)).fetchall()}


def member_allowed_connectors(sub: str, org_id: int) -> set:
    """Connecteurs (restreints) auxquels `sub` a droit dans l'org : ligne user=sub
    OU groupe ∈ ses groupes de l'org. (Un connecteur non restreint n'est pas listé
    ici mais reste ouvert — cf. org_restricted_connectors.)"""
    with _connect() as conn:
        return {r["connector"] for r in conn.execute(
            """
            SELECT DISTINCT a.connector FROM org_connector_access a
             WHERE a.org_id = %s AND (
                   (a.principal_type = 'user' AND a.principal_id = %s)
                OR (a.principal_type = 'group' AND a.principal_id IN (
                       SELECT m.group_id::text FROM org_group_members m
                         JOIN org_groups g ON g.id = m.group_id
                        WHERE m.sub = %s AND g.org_id = %s)))
            """,
            (org_id, sub, sub, org_id)).fetchall()}


def list_users_with_grants() -> list[dict]:
    """Pour /api/admin/users — chaque user + ses grants (sans api_key)."""
    users = list_users()
    out = []
    for u in users:
        u = dict(u)
        u["grants"] = list_grants_for_user(u["sub"])
        out.append(u)
    return out


# ── Schéma observé des connecteurs (rédaction de champs) ──────────────────────
def get_connector_schema(service: str) -> dict:
    """Squelette observé d'un connecteur (`{name: {type, paths:[...]}}`), {} si aucun."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT schema FROM connector_schemas WHERE service = %s", (service,)
        ).fetchone()
    return (row or {}).get("schema") or {}


def get_all_connector_schemas() -> dict:
    """Tous les schémas observés, `{service: {name: {type, paths}}}`."""
    with _connect() as conn:
        rows = conn.execute("SELECT service, schema FROM connector_schemas").fetchall()
    return {r["service"]: (r["schema"] or {}) for r in rows}


def upsert_connector_schema(service: str, schema: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO connector_schemas (service, schema, updated_at) "
            "VALUES (%s, %s::jsonb, NOW()) "
            "ON CONFLICT (service) DO UPDATE SET schema = EXCLUDED.schema, updated_at = NOW()",
            (service, json.dumps(schema)),
        )
