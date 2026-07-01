"""Comptes Unipile/messagerie, options comp (admin), pending hosted-auth.

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


def set_unipile_account(sub: str, account_id: str, account_name: Optional[str] = None,
                        org_id: Optional[int] = None, provider: str = "LINKEDIN") -> None:
    """Associe (upsert) le compte Unipile `account_id` à `(sub, provider)` (B3,
    multi-canal). `org_id` = org à laquelle ce compte est rattaché (ventilation par org)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO unipile_accounts (sub, provider, account_id, account_name, org_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sub, provider) DO UPDATE SET "
            "account_id = EXCLUDED.account_id, account_name = EXCLUDED.account_name, "
            "org_id = EXCLUDED.org_id, connected_at = NOW()",
            (sub, provider, account_id, account_name, org_id),
        )


def get_unipile_account_id(sub: str, provider: str = "LINKEDIN") -> Optional[str]:
    """`account_id` Unipile du user pour ce canal, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT account_id FROM unipile_accounts WHERE sub = %s AND provider = %s",
            (sub, provider),
        ).fetchone()
    return row["account_id"] if row else None


def get_unipile_feed_synced_at(sub: str, provider: str = "LINKEDIN") -> Optional[str]:
    """Horodatage (string ISO via row factory) du dernier sync du feed, ou None
    si jamais synchronisé / compte absent."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT feed_synced_at FROM unipile_accounts WHERE sub = %s AND provider = %s",
            (sub, provider),
        ).fetchone()
    return row["feed_synced_at"] if row else None


def touch_unipile_feed_synced(sub: str, provider: str = "LINKEDIN") -> None:
    """Marque le feed comme synchronisé maintenant (pose `feed_synced_at = NOW()`)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE unipile_accounts SET feed_synced_at = NOW() WHERE sub = %s AND provider = %s",
            (sub, provider),
        )


def get_unipile_account(sub: str, provider: str = "LINKEDIN") -> Optional[dict]:
    """Statut de connexion Unipile d'un canal (pour /api/me / dashboard) ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT provider, account_id, account_name, connected_at FROM unipile_accounts "
            "WHERE sub = %s AND provider = %s", (sub, provider)
        ).fetchone()
    return dict(row) if row else None


def list_unipile_accounts(sub: str) -> list[dict]:
    """Tous les comptes Unipile connectés du user, tous canaux confondus
    (`[{provider, account_id, account_name, org_id, connected_at}]`) — pour le dashboard.
    `org_id` = l'org à laquelle le compte est rattaché (ventilation par org, fiche admin)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT provider, account_id, account_name, org_id, connected_at FROM unipile_accounts "
            "WHERE sub = %s ORDER BY provider", (sub,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_unipile_account(sub: str, provider: str = "LINKEDIN") -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM unipile_accounts WHERE sub = %s AND provider = %s",
                     (sub, provider))


def count_unipile_accounts_for_org(org_id: int) -> int:
    """Nombre de comptes LinkedIn connectés rattachés à cet org
    (base du plafond anti-dérapage sur les comptes hébergés)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM unipile_accounts WHERE org_id = %s", (org_id,)
        ).fetchone()["n"]


def list_unipile_accounts_by_org() -> list[dict]:
    """`[{org_id, provider, account_id, sub}]` de tous les comptes rattachés à un org
    (org_id non NULL)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT org_id, provider, account_id, sub FROM unipile_accounts WHERE org_id IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def unipile_account_owners() -> list[dict]:
    """TOUS les comptes unipile mappés → propriétaire (sub/email) + org. Pour la vue
    admin « sièges de la clé plateforme » : réconcilier les comptes présents sur
    l'instance partagée avec leurs propriétaires oto (account_id NON mappé = orphelin)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ua.account_id, ua.provider, ua.account_name, ua.sub, u.email, "
            "ua.org_id, o.name AS org_name, ua.connected_at "
            "FROM unipile_accounts ua "
            "LEFT JOIN users u ON u.sub = ua.sub "
            "LEFT JOIN orgs o ON o.id = ua.org_id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_org_unipile_limit(org_id: int) -> Optional[int]:
    """Plafond de comptes Unipile de l'org (NULL = pas de plafond propre → défaut env)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT unipile_account_limit FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
    return row["unipile_account_limit"] if row else None


def set_org_unipile_limit(org_id: int, limit: Optional[int]) -> None:
    """Pose (ou efface, limit=None) le plafond de comptes Unipile d'un org."""
    with _connect() as conn:
        conn.execute(
            "UPDATE orgs SET unipile_account_limit = %s WHERE id = %s", (limit, org_id)
        )


def set_option_comp(entity_type: str, entity_id: str, option: str,
                    *, granted_by: Optional[str] = None) -> None:
    """Offre (comp gratuit) une option de connecteur à une entité user|org. Idempotent."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO option_comps (entity_type, entity_id, option, granted_by) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (entity_type, entity_id, option) "
            "DO UPDATE SET granted_by = EXCLUDED.granted_by, granted_at = NOW()",
            (entity_type, str(entity_id), option, granted_by),
        )


def clear_option_comp(entity_type: str, entity_id: str, option: str) -> bool:
    """Retire un comp d'option. True si une ligne a été supprimée."""
    with _connect() as conn:
        n = conn.execute(
            "DELETE FROM option_comps WHERE entity_type=%s AND entity_id=%s AND option=%s",
            (entity_type, str(entity_id), option),
        ).rowcount
    return n > 0


def has_option_comp(entity_type: str, entity_id: str, option: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM option_comps WHERE entity_type=%s AND entity_id=%s AND option=%s",
            (entity_type, str(entity_id), option),
        ).fetchone() is not None


def list_option_comps(entity_type: str, entity_id: str) -> list[str]:
    """Options offertes (comp) à cette entité — pour l'affichage admin."""
    with _connect() as conn:
        return [r["option"] for r in conn.execute(
            "SELECT option FROM option_comps WHERE entity_type=%s AND entity_id=%s",
            (entity_type, str(entity_id)),
        )]


def create_unipile_pending(nonce: str, sub: str, org_id: Optional[int] = None,
                           provider: str = "LINKEDIN") -> None:
    """Mappe un `nonce` (posé comme `name` sur le lien hosted-auth) au `(sub, provider)`
    (+ org actif), pour corréler au retour du webhook. Prune les nonces expirés (> 1h)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute("DELETE FROM unipile_pending WHERE created_at < NOW() - INTERVAL '1 hour'")
        conn.execute(
            "INSERT INTO unipile_pending (nonce, sub, org_id, provider) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (nonce) DO NOTHING",
            (nonce, sub, org_id, provider),
        )


def resolve_unipile_pending(nonce: str) -> Optional[dict]:
    """Consomme un nonce → `{sub, org_id, provider}` (et le supprime), ou None si inconnu/expiré."""
    with _connect() as conn:
        row = conn.execute(
            "DELETE FROM unipile_pending WHERE nonce = %s "
            "AND created_at >= NOW() - INTERVAL '1 hour' RETURNING sub, org_id, provider",
            (nonce,),
        ).fetchone()
    return dict(row) if row else None
