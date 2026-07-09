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
                        org_id: Optional[int] = None, provider: str = "LINKEDIN",
                        platform_seat: bool = False) -> None:
    """Associe (upsert) le compte Unipile `account_id` à `(sub, org_id, provider)`
    — scope MEMBRE (ADR 0033 B4) : le binding vaut DANS cette org. `platform_seat`
    = le compte consomme un siège de la clé plateforme (comptage/facturation par
    org) ; False en BYO."""
    if org_id is None:
        raise ValueError("org_id requis (scope membre, ADR 0033)")
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO unipile_accounts (sub, provider, account_id, account_name, org_id, platform_seat) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (sub, org_id, provider) DO UPDATE SET "
            "account_id = EXCLUDED.account_id, account_name = EXCLUDED.account_name, "
            "platform_seat = EXCLUDED.platform_seat, connected_at = NOW()",
            (sub, provider, account_id, account_name, org_id, platform_seat),
        )


def get_unipile_account_id(sub: str, org_id: Optional[int],
                           provider: str = "LINKEDIN") -> Optional[str]:
    """`account_id` Unipile du user pour ce canal DANS cette org, ou None.
    `org_id=None` (défensif) → None, jamais un repli org-agnostique."""
    if org_id is None:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT account_id FROM unipile_accounts "
            "WHERE sub = %s AND org_id = %s AND provider = %s",
            (sub, org_id, provider),
        ).fetchone()
    return row["account_id"] if row else None


def get_unipile_feed_synced_at(sub: str, org_id: Optional[int],
                               provider: str = "LINKEDIN") -> Optional[str]:
    """Horodatage (string ISO via row factory) du dernier sync du feed, ou None
    si jamais synchronisé / compte absent."""
    if org_id is None:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT feed_synced_at FROM unipile_accounts "
            "WHERE sub = %s AND org_id = %s AND provider = %s",
            (sub, org_id, provider),
        ).fetchone()
    return row["feed_synced_at"] if row else None


def touch_unipile_feed_synced(sub: str, org_id: Optional[int],
                              provider: str = "LINKEDIN") -> None:
    """Marque le feed comme synchronisé maintenant (pose `feed_synced_at = NOW()`)."""
    if org_id is None:
        return
    with _connect() as conn:
        conn.execute(
            "UPDATE unipile_accounts SET feed_synced_at = NOW() "
            "WHERE sub = %s AND org_id = %s AND provider = %s",
            (sub, org_id, provider),
        )


def get_unipile_account(sub: str, org_id: Optional[int],
                        provider: str = "LINKEDIN") -> Optional[dict]:
    """Statut de connexion Unipile d'un canal dans CETTE org, ou None."""
    if org_id is None:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT provider, account_id, account_name, connected_at FROM unipile_accounts "
            "WHERE sub = %s AND org_id = %s AND provider = %s", (sub, org_id, provider)
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


def clear_unipile_account(sub: str, org_id: Optional[int], provider: str = "LINKEDIN") -> None:
    if org_id is None:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM unipile_accounts WHERE sub = %s AND org_id = %s AND provider = %s",
                     (sub, org_id, provider))


def count_unipile_accounts_for_org(org_id: int) -> int:
    """Nombre de SIÈGES de la clé plateforme consommés par cet org (base du plafond
    anti-dérapage sur les comptes hébergés). Les comptes BYO (platform_seat=false)
    ne comptent pas — l'user paie sa propre instance Unipile."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM unipile_accounts WHERE org_id = %s AND platform_seat",
            (org_id,)
        ).fetchone()["n"]


def list_unipile_accounts_by_org() -> list[dict]:
    """`[{org_id, provider, account_id, sub}]` des comptes consommant un SIÈGE de la
    clé plateforme (ventilation facturation par org — les BYO sont exclus)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT org_id, provider, account_id, sub FROM unipile_accounts WHERE platform_seat"
        ).fetchall()
    return [dict(r) for r in rows]


def unipile_account_owners() -> list[dict]:
    """TOUS les comptes unipile mappés → propriétaire (sub/email) + org. Pour la vue
    admin « sièges de la clé plateforme » : réconcilier les comptes présents sur
    l'instance partagée avec leurs propriétaires oto (account_id NON mappé = orphelin)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ua.account_id, ua.provider, ua.account_name, ua.sub, u.email, "
            "ua.org_id, o.name AS org_name, ua.connected_at, ua.platform_seat "
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


def list_option_comps_for_option(option: str) -> list[dict]:
    """Bénéficiaires (entity_type, entity_id) d'une option donnée (comp) — l'inverse de
    `list_option_comps`, pour la vue admin connecteur-centrique (ADR 0044 §H : « qui a
    droit à ce connecteur au niveau plateforme »)."""
    with _connect() as conn:
        return [{"entity_type": r["entity_type"], "entity_id": r["entity_id"],
                 "granted_at": r["granted_at"]} for r in conn.execute(
            "SELECT entity_type, entity_id, granted_at FROM option_comps WHERE option=%s",
            (option,),
        )]


def create_unipile_pending(nonce: str, sub: str, org_id: Optional[int] = None,
                           provider: str = "LINKEDIN", platform_seat: bool = False) -> None:
    """Mappe un `nonce` (posé comme `name` sur le lien hosted-auth) au
    `(sub, org de contexte, provider)` + `platform_seat`, pour corréler au retour
    du webhook. Prune les nonces expirés (> 1h)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute("DELETE FROM unipile_pending WHERE created_at < NOW() - INTERVAL '1 hour'")
        conn.execute(
            "INSERT INTO unipile_pending (nonce, sub, org_id, provider, platform_seat) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (nonce) DO NOTHING",
            (nonce, sub, org_id, provider, platform_seat),
        )


def resolve_unipile_pending(nonce: str) -> Optional[dict]:
    """Consomme un nonce → `{sub, org_id, provider, platform_seat}` (et le supprime),
    ou None si inconnu/expiré."""
    with _connect() as conn:
        row = conn.execute(
            "DELETE FROM unipile_pending WHERE nonce = %s "
            "AND created_at >= NOW() - INTERVAL '1 hour' "
            "RETURNING sub, org_id, provider, platform_seat",
            (nonce,),
        ).fetchone()
    return dict(row) if row else None


def backfill_unipile_member_scope() -> dict:
    """One-shot idempotent (boot, ADR 0033 B4) : `unipile_accounts` passe au grain
    (sub, org, provider). Historique : `org_id` = « org porteuse du SIÈGE plateforme »
    (NULL en BYO) et la résolution l'ignorait → un compte LinkedIn connecté dans
    l'org A servait depuis l'org B. Désormais `org_id` = **org de contexte du
    binding** (toujours posée) et `platform_seat` garde la sémantique facturation.

    Étapes (gardées par l'état du PK — 'org_id' déjà dedans ⇒ no-op) :
    1. marquer `platform_seat=true` sur les lignes historiques à org_id posé
       (= mode plateforme, seules comptées par l'ancien plafond) ;
    2. org_id NULL (BYO/legacy) → org maison du sub ;
    3. verrou : NOT NULL + FK CASCADE + PK (sub, org_id, provider).
    S'exécute APRÈS backfill_personal_orgs (org maison garantie)."""
    from .. import org_store  # lazy (org_store importe credentials_store → db)
    with _connect() as conn:
        pk = conn.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'unipile_accounts'::regclass AND i.indisprimary"
        ).fetchall()
        if any(r["attname"] == "org_id" for r in pk):
            return {"done": True}
        conn.execute(
            "UPDATE unipile_accounts SET platform_seat = TRUE WHERE org_id IS NOT NULL")
        rows = conn.execute(
            "SELECT sub, provider FROM unipile_accounts WHERE org_id IS NULL").fetchall()
    moved = 0
    for r in rows:
        home = org_store.get_active_org(r["sub"])
        if home is None:
            logger.warning("backfill_unipile: pas d'org maison pour %s — skip", r["sub"])
            continue
        with _connect() as conn:
            conn.execute(
                "UPDATE unipile_accounts SET org_id = %s "
                "WHERE sub = %s AND provider = %s AND org_id IS NULL",
                (home, r["sub"], r["provider"]))
        moved += 1
    with _connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM unipile_accounts WHERE org_id IS NULL").fetchone()["n"]
        if remaining:
            logger.warning("backfill_unipile: %s lignes sans org — PK non migré (retry au prochain boot)",
                           remaining)
            return {"moved": moved, "remaining": remaining}
        conn.execute("ALTER TABLE unipile_accounts ALTER COLUMN org_id SET NOT NULL")
        conn.execute("ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_org_id_fkey")
        conn.execute("ALTER TABLE unipile_accounts ADD CONSTRAINT unipile_accounts_org_id_fkey "
                     "FOREIGN KEY (org_id) REFERENCES orgs(id) ON DELETE CASCADE")
        conn.execute("ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_pkey")
        conn.execute("ALTER TABLE unipile_accounts ADD PRIMARY KEY (sub, org_id, provider)")
    logger.info("backfill_unipile_member_scope: moved=%s, PK migré", moved)
    return {"moved": moved, "remaining": 0}
