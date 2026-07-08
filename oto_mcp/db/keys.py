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

from ._conn import KEY_PROVIDERS, CREDENTIAL_PROVIDERS, _connect
from .users import list_users, upsert_user


def _check_provider(provider: str) -> None:
    # Accepte tout provider pouvant détenir un credential : keyed (clé API), byo
    # multi-champs, ET sessions navigateur cookie (brevo/crunchbase/pennylaneged, qui
    # persistent leur Context Browserbase via ce chemin). KEY_PROVIDERS seul (keyed) est
    # trop étroit → rejetait la persistance de session (« Unknown provider 'pennylaneged' »).
    if provider not in CREDENTIAL_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (allowed: {sorted(CREDENTIAL_PROVIDERS)})")


# Scope MEMBRE (ADR 0033) : la clé per-user est keyée (sub, org) — « ma clé dans
# CETTE org ». L'org est TOUJOURS passée par l'appelant (access/api_routes résolvent
# le contexte via le seam `current_org` ; la couche db ne le lit jamais elle-même).
# `org_id=None` (défensif, contexte org introuvable) → pas de clé, jamais un repli
# org-agnostique : c'était le trou que 0033 ferme.

# `account` discrimine le multi-compte (ADR 0033 étendu) : plusieurs credentials du
# MÊME connecteur pour un même (sub, org) — ex. « 2 Zoho ». '' = mono-compte legacy
# (déchiffrable sans migration, l'AAD n'ajoute le segment que s'il est non vide). La
# SÉLECTION du compte (défaut projet / défaut membre / auto) vit dans access ; ici,
# passe-plat pur vers le coffre déjà multi-compte.

def set_member_api_key(sub: str, org_id: int, provider: str, key: str,
                       account: str = "") -> None:
    _check_provider(provider)
    upsert_user(sub)
    # Coffre chiffré, source unique. Import lazy (db ne doit pas importer
    # credentials_store au niveau module — cycle).
    from .. import credentials_store
    credentials_store.set_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub),
        provider, key, set_by=sub, account=account)


def clear_member_api_key(sub: str, org_id: int, provider: str,
                         account: str = "") -> bool:
    _check_provider(provider)
    from .. import credentials_store
    return credentials_store.clear_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub), provider,
        account=account)


def get_member_api_key(sub: str, org_id: Optional[int], provider: str,
                       account: str = "") -> Optional[str]:
    # Lit le coffre `connector_credentials` (déchiffre — chemin de RÉSOLUTION).
    # Import lazy (anti-cycle) ; require_keyed dans le store.
    if org_id is None:
        return None
    from .. import credentials_store
    return credentials_store.get_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub), provider,
        account=account)


def has_member_api_key(sub: str, org_id: Optional[int], provider: str,
                       account: Optional[str] = None) -> bool:
    """Présence de la clé du membre dans CETTE org, SANS déchiffrer (status_for).
    `account=None` = n'importe quel compte (présence du connecteur, multi-compte
    inclus) ; '' = strictement le mono-compte ; une valeur = ce compte précis."""
    if org_id is None:
        return False
    from .. import credentials_store
    return credentials_store.has_credential(
        credentials_store.MEMBER, credentials_store.member_id(org_id, sub), provider,
        account=account)


def _grants_for_scope(scope: str) -> list[dict]:
    """ADR 0044 §F : grants dérivés des instances plateforme dont le `share_down` contient
    `scope` (`user:<sub>` | `org:<id>`). Renvoie {provider, label, daily_quota} — plus de
    surrogate platform_key_id. `share_down @> [scope]` = contenance JSONB (indexable)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector AS provider, entity_id AS label, meta FROM connector_credentials "
            "WHERE entity_type = 'platform' AND share_down @> %s::jsonb ORDER BY connector",
            (json.dumps([scope]),)).fetchall()
    out = []
    for r in rows:
        rlb = (r["meta"] or {}).get("rate_limit_by") or {}
        out.append({"provider": r["provider"], "label": r["label"],
                    "daily_quota": rlb.get(scope)})
    return out


def list_grants_for_user(sub: str) -> list[dict]:
    """Grants d'un user (ADR 0044 §F : dérivés du share_down des instances plateforme)."""
    return _grants_for_scope(f"user:{sub}")


def list_org_grants(org_id: int) -> list[dict]:
    """Grants d'une org (ADR 0044 §F : dérivés du share_down des instances plateforme)."""
    return _grants_for_scope(f"org:{org_id}")


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


# ── ACL connecteur au grain ÉQUIPE (ADR 0012 B2, restrict-only) ─────────────
def set_group_connector_access(group_id: int, connector: str, principal_sub: str,
                               granted_by: Optional[str] = None) -> None:
    """Autorise un MEMBRE (`sub`) sur un connecteur dans l'équipe → le rend RESTREINT
    dans l'équipe (deny-by-default) s'il ne l'était pas. Idempotent."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO group_connector_access (group_id, connector, principal_sub, granted_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (group_id, connector, principal_sub) DO NOTHING
            """,
            (group_id, connector, principal_sub, granted_by),
        )


def clear_group_connector_access(group_id: int, connector: str, principal_sub: str) -> None:
    """Retire un membre. Dernière ligne partie ⟹ connecteur ré-ouvert à toute l'équipe."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM group_connector_access WHERE group_id = %s AND connector = %s "
            "AND principal_sub = %s",
            (group_id, connector, principal_sub),
        )


def list_group_connector_access(group_id: int, connector: Optional[str] = None) -> list[dict]:
    """ACL connecteur de l'équipe : [{connector, principal_sub, granted_by, granted_at}]."""
    sql = ("SELECT connector, principal_sub, granted_by, granted_at "
           "FROM group_connector_access WHERE group_id = %s")
    args: tuple = (group_id,)
    if connector is not None:
        sql += " AND connector = %s"
        args = (group_id, connector)
    sql += " ORDER BY connector, principal_sub"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def group_restricted_connectors(group_id: int) -> set:
    """Connecteurs RESTREINTS dans l'équipe (≥1 ligne d'ACL) — deny-by-default pour eux."""
    with _connect() as conn:
        return {r["connector"] for r in conn.execute(
            "SELECT DISTINCT connector FROM group_connector_access WHERE group_id = %s",
            (group_id,)).fetchall()}


def group_member_allowed_connectors(sub: str, group_id: int) -> set:
    """Connecteurs (restreints dans l'équipe) auxquels `sub` a droit (ligne à son nom)."""
    with _connect() as conn:
        return {r["connector"] for r in conn.execute(
            "SELECT connector FROM group_connector_access WHERE group_id = %s AND principal_sub = %s",
            (group_id, sub)).fetchall()}


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
