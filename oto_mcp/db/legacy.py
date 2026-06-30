"""Store PostgreSQL — fonctions de domaine (reliquat de l'ex-monolithe `db.py`).

⚠️ Transitoire. La plomberie (pool/connexion), la DDL (`_SCHEMA`) et l'init ont
été extraites en `_conn`/`_schema`/`_init` (barreau 2). Les fonctions ci-dessous
seront réparties par domaine (users, connectors, usage, …) aux barreaux suivants.
Le package `db/__init__` ré-exporte tout → la surface `db.<symbole>` est inchangée.
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

from ._conn import _connect, KEY_PROVIDERS

logger = logging.getLogger(__name__)

def upsert_user(sub: str, email: Optional[str] = None, name: Optional[str] = None,
                iss: Optional[str] = None) -> None:
    """Create the user row if missing, refresh email/name if known.

    Fédération de compte (otomata#16) : à la **première** création (vrai INSERT),
    on provisionne le compte memento correspondant par email (best-effort, non
    bloquant — cf. `memento_federation`). Le `(xmax = 0)` distingue insert/update
    sans SELECT préalable : 0 sur une ligne fraîchement insérée, ≠ 0 sur un UPDATE.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO users (sub, email, name)
            VALUES (%s, %s, %s)
            ON CONFLICT(sub) DO UPDATE SET
                email = COALESCE(EXCLUDED.email, users.email),
                name  = COALESCE(EXCLUDED.name,  users.name),
                updated_at = NOW()
            RETURNING (xmax = 0) AS inserted
            """,
            (sub, email, name),
        ).fetchone()
    if row and row.get("inserted") and email:
        # Réconciliation invitation↔signup (ADR 0013) : un invité qui s'inscrit
        # (par n'importe quel chemin, pas seulement le lien /invite) voit son
        # invitation en attente honorée par l'email vérifié → il saute la waitlist
        # au lieu d'y rester coincé avec une invitation orpheline. Synchrone (une
        # fois, au 1er insert) mais best-effort : un échec ne casse pas l'auth.
        try:
            from .. import org_store
            org_store.reconcile_signup_with_invitation(sub, email)
        except Exception:
            pass
        # Import paresseux : la fédération est optionnelle (no-op sans secret), et
        # on ne veut pas de dépendance dure au boot. Jamais bloquant / jamais fatal.
        from .. import memento_federation
        memento_federation.provision_async(sub, email)
    if row and row.get("inserted"):
        # Suppression du perso (otomata-private) : tout user a TOUJOURS une org maison.
        # Si l'inscription ne l'a pas déjà rattaché à une org (invitation/referral
        # ci-dessus), on lui crée son espace. Idempotent, best-effort, hors gate email.
        try:
            from .. import org_store
            org_store.ensure_personal_org(sub, email=email, name=name)
        except Exception:
            pass
    # Bascule de tenant (B1, otomata#35) : sur un login du NOUVEAU tenant, fusionner
    # l'ancien compte (même email) → ce sub. Gaté par env `OTO_MCP_TENANT_MIGRATION_ISS`
    # (dormant hors fenêtre de bascule). Idempotent, best-effort, à chaque login
    # new-tenant (pas que au 1er insert → couvre les retries / l'ordre des logins).
    # ⚠️ SÉCU (account takeover) : la décision de merge se prend sur l'email
    # AUTORITATIF lu de Logto (Management API), JAMAIS sur le claim email/email_verified
    # du token — un token forgé pourrait revendiquer l'email d'autrui pour absorber son
    # compte (rôle, coffre). reconcile_tenant_migration récupère lui-même cet email ;
    # le claim `email` n'est passé que comme PRÉ-FILTRE cheap (éviter un appel Logto à
    # chaque requête quand rien ne matche).
    if iss:
        _mig = os.environ.get("OTO_MCP_TENANT_MIGRATION_ISS", "").strip().rstrip("/")
        if _mig and iss.rstrip("/") == _mig:
            try:
                reconcile_tenant_migration(sub, email_hint=email)
            except Exception:
                pass


def get_user(sub: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE sub = %s", (sub,)).fetchone()
        return dict(row) if row else None


# --- Bascule de tenant Logto (B1, otomata#35) -------------------------------
# Inventaire des colonnes keyed-by-sub à repointer (issue oto-backend#56). Plain
# UPDATE : le nouveau sub est frais → aucun conflit de PK, SAUF user_account_profile
# (PK sub) et connector_credentials (coffre user), traités à part.
_SUB_COLUMNS = [
    # données de l'user
    ("usage", "sub"), ("tool_calls", "sub"), ("usage_signals", "sub"),
    ("user_disabled_tools", "sub"), ("user_enabled_tools", "sub"), ("user_presets", "sub"),
    ("user_grants", "sub"), ("user_datastores", "sub"),
    ("datastore_shares", "owner_sub"), ("datastore_shares", "shared_with_sub"),
    ("org_members", "sub"), ("org_group_members", "sub"),
    ("user_api_tokens", "sub"), ("unipile_accounts", "sub"), ("unipile_pending", "sub"),
    # attribution (soft)
    ("users", "invited_by"), ("user_grants", "granted_by"),
    ("orgs", "created_by"),
    ("org_invitations", "invited_by"), ("org_invitations", "accepted_sub"),
    ("org_groups", "created_by"), ("org_instructions", "set_by"),
    ("org_instruction_revisions", "set_by"), ("org_group_instructions", "set_by"),
    ("org_group_instruction_revisions", "set_by"), ("doctrine_library", "published_by"),
]


def resolve_sub(sub: str) -> str:
    """Canonicalise un sub via sub_aliases (vieux token d'un tenant en drain →
    compte migré). Renvoie le sub inchangé si pas d'alias (cas normal)."""
    if not sub:
        return sub
    try:
        with _connect() as conn:
            row = conn.execute("SELECT new_sub FROM sub_aliases WHERE old_sub=%s", (sub,)).fetchone()
        return row["new_sub"] if row else sub
    except Exception:
        return sub


_ROLE_RANK = {"member": 0, "admin": 1, "super_admin": 2}


def _stronger_role(a: Optional[str], b: Optional[str]) -> str:
    """Le plus haut des deux rôles (une fusion n'enlève pas un privilège)."""
    ra, rb = _ROLE_RANK.get(a or "member", 0), _ROLE_RANK.get(b or "member", 0)
    return (a if ra >= rb else b) or "member"


def _merge_access_status(a: Optional[str], b: Optional[str]) -> str:
    """Statut d'accès fusionné, sans rétrograder : `blocked` (deny explicite) prime,
    sinon `active` prime sur `pending`."""
    s = {a, b}
    if "blocked" in s:
        return "blocked"
    if "active" in s:
        return "active"
    return "pending"


def migrate_sub(old_sub: str, new_sub: str) -> bool:
    """MERGE transactionnel ancien→nouveau compte (bascule de tenant, issue #56).
    Hérite les champs d'accès de l'ancien, repointe TOUTES les tables keyed-by-sub
    (les 3 FK `ON DELETE CASCADE` incluses, AVANT de supprimer l'ancien → pas de
    cascade destructrice), supprime l'ancienne ligne users, pose l'alias. Idempotent
    (no-op si l'ancien sub n'existe plus). True si une migration a eu lieu."""
    if not old_sub or not new_sub or old_sub == new_sub:
        return False
    with _connect() as conn:
        old = conn.execute("SELECT * FROM users WHERE sub=%s", (old_sub,)).fetchone()
        if not old:
            return False  # déjà migré / inexistant
        # 1. fusionner les champs d'accès SANS JAMAIS RÉTROGRADER. Une fusion ne doit
        #    pas réduire l'accès : on prend le rôle le plus fort, le statut le plus
        #    permissif (active > pending ; blocked reste un deny explicite), le quota
        #    max. ⚠️ Le naïf « hérite de l'ancien » downgrade le nouveau si l'ancien est
        #    un stub frais (member/pending) re-fusionné par-dessus un compte établi
        #    (vécu 2026-06-23 : alexis super_admin/active repassé member/pending).
        new = conn.execute(
            "SELECT role, access_status, invite_quota FROM users WHERE sub=%s", (new_sub,)
        ).fetchone() or {}
        conn.execute(
            """UPDATE users SET
                 role = %(role)s, access_status = %(st)s, invite_quota = %(q)s,
                 invited_by = COALESCE(users.invited_by, %(ib)s),
                 access_granted_at = COALESCE(users.access_granted_at, %(ag)s),
                 avatar_url = COALESCE(users.avatar_url, %(av)s), updated_at = NOW()
               WHERE sub = %(new)s""",
            {"role": _stronger_role(old["role"], new.get("role")),
             "st": _merge_access_status(old["access_status"], new.get("access_status")),
             "q": max(old["invite_quota"] or 0, new.get("invite_quota") or 0),
             "ib": old.get("invited_by"), "ag": old.get("access_granted_at"),
             "av": old.get("avatar_url"), "new": new_sub},
        )
        # 2. user_account_profile (PK sub) : retirer le frais du new PUIS repointer
        #    l'ancien (garde l'historique d'onboarding). DELETE d'abord → pas de conflit PK.
        conn.execute("DELETE FROM user_account_profile WHERE sub=%s", (new_sub,))
        conn.execute("UPDATE user_account_profile SET sub=%s WHERE sub=%s", (new_sub, old_sub))
        # 3. repointer toutes les colonnes sub.
        for table, col in _SUB_COLUMNS:
            conn.execute(f"UPDATE {table} SET {col}=%s WHERE {col}=%s", (new_sub, old_sub))
        # coffre user (connector_credentials) : entité + auteur.
        conn.execute("UPDATE connector_credentials SET entity_id=%s WHERE entity_type='user' AND entity_id=%s", (new_sub, old_sub))
        conn.execute("UPDATE connector_credentials SET set_by=%s WHERE set_by=%s", (new_sub, old_sub))
        # 4. supprimer l'ancienne ligne users (enfants FK déjà repointés).
        conn.execute("DELETE FROM users WHERE sub=%s", (old_sub,))
        # 5. alias (drain des vieux tokens → compte canonique).
        conn.execute(
            "INSERT INTO sub_aliases (old_sub, new_sub) VALUES (%s,%s) "
            "ON CONFLICT (old_sub) DO UPDATE SET new_sub=EXCLUDED.new_sub, migrated_at=NOW()",
            (old_sub, new_sub),
        )
    logger.info("tenant migration: merged %s → %s (par email)", old_sub, new_sub)
    return True


def reconcile_tenant_migration(new_sub: str, email_hint: Optional[str] = None) -> bool:
    """Au login sur le nouveau tenant : récupère l'email AUTORITATIF du compte depuis
    Logto (Management API — le `primaryEmail` n'existe qu'après vérification, donc
    fiable même si le token ment) puis, si EXACTEMENT un autre compte partage cet email
    (l'ancien sub), le migre vers new_sub. No-op si email introuvable, 0 (rien à migrer)
    ou >1 (ambigu — on ne touche pas). Idempotent (l'ancien disparaît après migration).

    `email_hint` (claim email du token) n'est qu'un PRÉ-FILTRE pour éviter un appel
    Logto à chaque requête : si aucun autre compte ne porte cet email, rien à migrer →
    on ne sollicite pas Logto. Il n'entre JAMAIS dans la décision de merge (sécurité)."""
    if not new_sub:
        return False
    try:
        # Pré-filtre cheap sur le claim (non fiable) : court-circuite le cas courant
        # (déjà migré / rien à fusionner) sans round-trip Logto.
        if email_hint:
            with _connect() as conn:
                pre = conn.execute(
                    "SELECT 1 FROM users WHERE lower(email)=lower(%s) AND sub<>%s LIMIT 1",
                    (email_hint, new_sub),
                ).fetchone()
            if not pre:
                return False
        # Email AUTORITATIF (source de vérité) — la décision de merge se prend ici.
        from ..oauth_facade import logto_user_primary_email
        email = logto_user_primary_email(new_sub)
        if not email:
            return False
        with _connect() as conn:
            rows = conn.execute(
                "SELECT sub FROM users WHERE lower(email)=lower(%s) AND sub<>%s",
                (email, new_sub),
            ).fetchall()
        if len(rows) != 1:
            return False
        return migrate_sub(rows[0]["sub"], new_sub)
    except Exception:
        logger.warning("reconcile_tenant_migration échoué pour %s", new_sub, exc_info=True)
        return False


# --- accès plateforme & quota d'invitation (ADR 0013) -----------------------

def grant_platform_access(sub: str, *, invited_by: Optional[str] = None,
                          quota: Optional[int] = None) -> None:
    """Passe le compte en 'active' (alpha). Idempotent sur access_granted_at et
    invited_by (COALESCE — ne réécrase pas un parrain déjà posé). `quota` crédite
    le budget referral (referral alpha) ; None = ne touche pas au quota (cas
    org-invite : le membre obtient l'accès mais pas de budget d'invitation)."""
    sets = ["access_status = 'active'",
            "access_granted_at = COALESCE(access_granted_at, NOW())",
            "updated_at = NOW()"]
    params: list = []
    if quota is not None:
        sets.append("invite_quota = %s")
        params.append(int(quota))
    if invited_by is not None:
        sets.append("invited_by = COALESCE(invited_by, %s)")
        params.append(invited_by)
    params.append(sub)
    with _connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE sub = %s", tuple(params))


def block_platform_access(sub: str) -> None:
    """Passe le compte en 'blocked' (rejet d'un cold signup indésirable). Le compte
    sort de la waitlist (qui ne liste que 'pending') et `session_visibility` le
    traite comme non-'active' (allowlist onboarding only). Réversible : un
    `grant_platform_access` ultérieur le repasse 'active'. Ne touche pas au quota."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET access_status = 'blocked', updated_at = NOW() WHERE sub = %s",
            (sub,),
        )


def consume_invite_quota(sub: str) -> bool:
    """Décrémente atomiquement le quota referral si > 0. True si consommé, False
    si épuisé (WHERE invite_quota > 0 → pas de course)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET invite_quota = invite_quota - 1, updated_at = NOW() "
            "WHERE sub = %s AND invite_quota > 0",
            (sub,),
        )
        return (cur.rowcount or 0) > 0


def refund_invite_quota(sub: str) -> None:
    """Re-crédite une invitation (rollback si la création échoue après consume)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET invite_quota = invite_quota + 1, updated_at = NOW() WHERE sub = %s",
            (sub,),
        )


def set_invite_quota(sub: str, quota: int) -> None:
    """Fixe le quota referral (admin top-up). Ne change pas l'access_status."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET invite_quota = %s, updated_at = NOW() WHERE sub = %s",
            (int(quota), sub),
        )


def list_waitlist() -> list[dict]:
    """Comptes en attente (cold signups non approuvés), du plus ancien au plus
    récent — la file d'attente est une vue dérivée, pas une table."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, email, name, created_at FROM users "
            "WHERE access_status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


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


# --- avatar -----------------------------------------------------------------

def set_avatar_url(sub: str, url: Optional[str]) -> None:
    """Pose (ou efface si url=None) l'URL publique de l'avatar du user.

    URL publique servie depuis l'Object Storage — pas un secret, colonne en
    clair (hors coffre chiffré)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET avatar_url = %s, updated_at = NOW() WHERE sub = %s",
            (url, sub),
        )


# --- LinkedIn ---------------------------------------------------------------

def set_unipile_account(sub: str, account_id: str, account_name: Optional[str] = None,
                        org_id: Optional[int] = None, provider: str = "LINKEDIN") -> None:
    """Associe (upsert) le compte Unipile `account_id` à `(sub, provider)` (B3,
    multi-canal). `org_id` = org dont l'abonnement porte ce compte (compté + facturé)."""
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
    `org_id` = l'org dont l'abonnement porte le compte (ventilation par org, fiche admin)."""
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
    """Nombre de comptes LinkedIn connectés portés par l'abonnement de cet org
    (base du plafond anti-dérapage + de la facturation par compte)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM unipile_accounts WHERE org_id = %s", (org_id,)
        ).fetchone()["n"]


def list_unipile_accounts_by_org() -> list[dict]:
    """`[{org_id, provider, account_id, sub}]` de tous les comptes rattachés à un org
    (org_id non NULL) — itéré par la facturation récurrente."""
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


# --- abonnements récurrents Stripe par org (option LinkedIn €15/mois/siège) ----

def get_org_subscription(org_id: int, product: str) -> Optional[dict]:
    """Miroir local de l'abonnement Stripe `product` de l'org (status/quantity/ids)
    ou None. Lu pour le gate d'activation (sans appel Stripe par requête)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id, product, stripe_customer_id, stripe_subscription_id, "
            "status, quantity, updated_at FROM org_subscriptions "
            "WHERE org_id = %s AND product = %s", (org_id, product)
        ).fetchone()
    return dict(row) if row else None


def upsert_org_subscription(org_id: int, product: str, *, status: str,
                            stripe_customer_id: Optional[str] = None,
                            stripe_subscription_id: Optional[str] = None,
                            quantity: Optional[int] = None) -> None:
    """Upsert le miroir d'abonnement (appelé par les webhooks Stripe). Les champs
    ids/quantity laissés à None ne sont pas écrasés s'ils existent déjà."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO org_subscriptions "
            "(org_id, product, status, stripe_customer_id, stripe_subscription_id, quantity, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, 0), NOW()) "
            "ON CONFLICT (org_id, product) DO UPDATE SET "
            "status = EXCLUDED.status, "
            "stripe_customer_id = COALESCE(EXCLUDED.stripe_customer_id, org_subscriptions.stripe_customer_id), "
            "stripe_subscription_id = COALESCE(EXCLUDED.stripe_subscription_id, org_subscriptions.stripe_subscription_id), "
            "quantity = COALESCE(%s, org_subscriptions.quantity), updated_at = NOW()",
            (org_id, product, status, stripe_customer_id, stripe_subscription_id,
             quantity, quantity),
        )


def get_org_by_subscription_id(stripe_subscription_id: str) -> Optional[dict]:
    """Retrouve `{org_id, product}` depuis l'id d'abonnement Stripe (webhooks dont
    le metadata serait absent)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id, product FROM org_subscriptions WHERE stripe_subscription_id = %s",
            (stripe_subscription_id,)
        ).fetchone()
    return dict(row) if row else None


# Comps d'options (gratuit, posé par un admin) — contrepartie de `org_subscriptions`,
# lues par `access.has_option` (seam unique, couche 3 du modèle de connecteur).

def set_option_comp(entity_type: str, entity_id: str, option: str,
                    *, granted_by: Optional[str] = None) -> None:
    """Offre (comp gratuit) une option payante à une entité user|org. Idempotent."""
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


# Crunchbase = connecteur `personal_session` standard (coffre `crunchbase` via
# set_user_api_key / resolve_credential), le Context Browserbase tenant lieu de
# credential (ADR 0026). Plus de fonctions de session cookies+UA dédiées.


# --- onboarding / account profile -------------------------------------------

def get_account_profile(sub: str) -> dict:
    """Fiche d'onboarding de l'user : {onboarded, profile, onboarded_at, updated_at}.

    Jamais None — un sub sans ligne renvoie l'état par défaut (non onboardé,
    profile vide). Lecture seule (ne crée pas la ligne)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT onboarded, profile, onboarded_at, updated_at "
            "FROM user_account_profile WHERE sub = %s",
            (sub,),
        ).fetchone()
    if not row:
        return {"onboarded": False, "profile": {}, "onboarded_at": None, "updated_at": None}
    profile = row["profile"]
    if isinstance(profile, str):  # selon le driver, JSONB peut revenir en texte
        try:
            profile = json.loads(profile)
        except Exception:
            profile = {}
    return {
        "onboarded": bool(row["onboarded"]),
        "profile": profile or {},
        "onboarded_at": row["onboarded_at"],
        "updated_at": row["updated_at"],
    }


def update_account_profile(
    sub: str, fields: Optional[dict] = None, onboarded: Optional[bool] = None,
) -> dict:
    """Met à jour la fiche d'onboarding (upsert). `fields` est **shallow-mergé**
    dans le JSONB `profile` (clés existantes écrasées, les autres conservées).
    `onboarded` (si fourni) bascule le booléan + stampe `onboarded_at` au passage
    à vrai. Renvoie l'état résultant (comme `get_account_profile`)."""
    upsert_user(sub)
    patch = json.dumps(fields or {})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_account_profile (sub, profile, onboarded, onboarded_at, updated_at)
            VALUES (
                %s,
                %s::jsonb,
                COALESCE(%s, FALSE),
                CASE WHEN %s IS TRUE THEN NOW() ELSE NULL END,
                NOW()
            )
            ON CONFLICT (sub) DO UPDATE SET
                profile = user_account_profile.profile || EXCLUDED.profile,
                onboarded = COALESCE(%s, user_account_profile.onboarded),
                onboarded_at = CASE
                    WHEN %s IS TRUE AND user_account_profile.onboarded_at IS NULL THEN NOW()
                    WHEN %s IS FALSE THEN NULL
                    ELSE user_account_profile.onboarded_at
                END,
                updated_at = NOW()
            """,
            (sub, patch, onboarded, onboarded, onboarded, onboarded, onboarded),
        )
    return get_account_profile(sub)


# --- user API keys ----------------------------------------------------------

def _check_provider(provider: str) -> None:
    if provider not in KEY_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (allowed: {KEY_PROVIDERS})")


def set_user_api_key(sub: str, provider: str, key: str) -> None:
    _check_provider(provider)
    upsert_user(sub)
    # Coffre chiffré, source unique. Import lazy (db ne doit pas importer
    # credentials_store au niveau module — cycle).
    from .. import credentials_store
    credentials_store.set_credential("user", sub, provider, key, set_by=sub)


def clear_user_api_key(sub: str, provider: str) -> None:
    _check_provider(provider)
    from .. import credentials_store
    credentials_store.clear_credential("user", sub, provider)


def get_user_api_key(sub: str, provider: str) -> Optional[str]:
    # Lit le coffre `connector_credentials` (déchiffre — chemin de RÉSOLUTION).
    # Import lazy (anti-cycle) ; require_keyed dans le store.
    from .. import credentials_store
    return credentials_store.get_credential("user", sub, provider)


def has_user_api_key(sub: str, provider: str) -> bool:
    """Présence d'une clé perso SANS la déchiffrer (status_for / /api/me)."""
    from .. import credentials_store
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

def insert_tool_call(row: dict) -> None:
    """Sink otomata-calllog : insère un row canonique (server, sub, email, tool,
    args, ok, error, duration_ms) + corrélation OTO-LOCALE (session_id, run_id ;
    ADR 0017, absents du contrat canonique → enrichis par le sink). Best-effort
    côté middleware — jamais bloquant."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tool_calls
                (server, sub, email, tool, args, ok, error, duration_ms, session_id, run_id, org_id)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
            """,
            (
                row.get("server") or "oto", row.get("sub"), row.get("email"),
                row["tool"], json.dumps(row.get("args")) if row.get("args") is not None else None,
                bool(row.get("ok")), row.get("error"), row.get("duration_ms"),
                row.get("session_id"), row.get("run_id"), row.get("org_id"),
            ),
        )


# --- Runs / déroulés (ADR 0017, métadonnée persistée) -----------------------

def insert_run(
    run_id: str, *, sub: Optional[str], org_id: Optional[int], label: str,
    doctrine: Optional[str] = None,
) -> None:
    """Persiste l'ouverture d'un run (best-effort, idempotent sur `run_id`). La pile
    session-scopée de `doctrine_run.py` reste la source du run ACTIF ; cette ligne
    est la trace durable (label/doctrine)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, sub, org_id, label, doctrine) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
            (run_id, sub, org_id, label, doctrine),
        )


def finish_run(run_id: str, outcome: str, note: Optional[str] = None) -> None:
    """Clôt un run persisté (outcome + note + finished_at). No-op si run_id inconnu
    (run ouvert dans une session sans persistance, ou déjà prune)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET outcome = %s, note = %s, finished_at = NOW() WHERE run_id = %s",
            (outcome, note, run_id),
        )


def recent_runs(sub: str, org_id: Optional[int], limit: int = 5) -> list[dict]:
    """Les `limit` derniers runs d'un (sub, org), plus récent d'abord. Sert
    l'anticipation du contexte injecté (#50 bloc C) + la boucle d'usage."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, label, doctrine, outcome, started_at, finished_at "
            "FROM runs WHERE sub = %s AND org_id IS NOT DISTINCT FROM %s "
            "ORDER BY started_at DESC LIMIT %s",
            (sub, org_id, limit),
        ).fetchall()
    return list(rows)


# --- Instructions plateforme (#50, blocs A/B éditables) ---------------------

def get_platform_instruction(key: str) -> Optional[dict]:
    """Le bloc plateforme `key` ('secret_sauce'|'onboarding') ou None s'il n'a
    jamais été seedé. `{key, body_md, updated_at, updated_by}`."""
    with _connect() as conn:
        return conn.execute(
            "SELECT key, body_md, updated_at, updated_by FROM platform_instructions WHERE key = %s",
            (key,),
        ).fetchone()


def list_platform_instructions() -> list[dict]:
    """Tous les blocs plateforme (surface admin)."""
    with _connect() as conn:
        return list(conn.execute(
            "SELECT key, body_md, updated_at, updated_by FROM platform_instructions ORDER BY key"
        ).fetchall())


def set_platform_instruction(key: str, body_md: str, updated_by: Optional[str] = None) -> None:
    """Upsert d'un bloc plateforme (édition admin)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO platform_instructions (key, body_md, updated_at, updated_by) "
            "VALUES (%s, %s, NOW(), %s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "body_md = EXCLUDED.body_md, updated_at = NOW(), updated_by = EXCLUDED.updated_by",
            (key, body_md, updated_by),
        )


def seed_platform_instruction(key: str, body_md: str) -> None:
    """Pose le défaut d'un bloc plateforme s'il n'existe pas encore (boot, idempotent).
    Ne touche PAS un bloc déjà édité par l'admin."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO platform_instructions (key, body_md) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (key, body_md),
        )


# --- Signaux d'usage volontaires (ADR 0017, barreau 3) ----------------------

def insert_usage_signal(
    *, sub: Optional[str], org_id: Optional[int], signal: str, kind: str,
    target: Optional[str], body: Optional[str], session_id: Optional[str],
    source: str = "agent",
) -> int:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO usage_signals
                (sub, org_id, signal, kind, target, body, session_id, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (sub, org_id, signal, kind, target, body, session_id, source),
        ).fetchone()
        return int(row["id"])


def list_usage_signals(
    signal: Optional[str] = None, target: Optional[str] = None, limit: int = 200,
    status: Optional[str] = None,
) -> list[dict]:
    """Signaux récents (récent d'abord), filtrables par type / cible / statut —
    base des projections (qualité d'outil, manques) du barreau 4.

    status: 'open' (resolved_at IS NULL) | 'resolved' (NOT NULL) | None (tous)."""
    limit = max(1, min(int(limit), 1000))
    sql = ("SELECT id, created_at, sub, org_id, signal, kind, target, body, "
           "session_id, source, resolved_at, resolved_by, resolution "
           "FROM usage_signals")
    clauses, params = [], []
    if signal:
        clauses.append("signal = %s"); params.append(signal)
    if target:
        clauses.append("target = %s"); params.append(target)
    if status == "open":
        clauses.append("resolved_at IS NULL")
    elif status == "resolved":
        clauses.append("resolved_at IS NOT NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def resolve_usage_signal(
    signal_id: int, *, resolved_by: Optional[str], note: Optional[str] = None,
    resolved: bool = True,
) -> Optional[dict]:
    """Marque un signal traité (ou le ré-ouvre si resolved=False). Renvoie la row
    mise à jour, ou None si l'id n'existe pas."""
    with _connect() as conn:
        if resolved:
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET resolved_at = NOW(), resolved_by = %s, resolution = %s
                 WHERE id = %s
                RETURNING id, signal, kind, target, resolved_at, resolved_by, resolution
                """,
                (resolved_by, note, signal_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET resolved_at = NULL, resolved_by = NULL, resolution = NULL
                 WHERE id = %s
                RETURNING id, signal, kind, target, resolved_at, resolved_by, resolution
                """,
                (signal_id,),
            ).fetchone()
        return dict(row) if row else None


# --- Projections « runs / usage » (ADR 0017, barreau 4) ----------------------
# Lecture seule, dérivées de tool_calls (run_id stampé) + usage_signals. Le
# `label`/`doctrine`/`outcome` d'un run viennent des appels run_start/run_finish.

def list_runs(limit: int = 100) -> list[dict]:
    """Runs récents (un par run_id ouvert via run_start) avec label/doctrine,
    acteur, bornes, outcome (si fermé) et nb d'appels du déroulé. `slug` (alias =
    doctrine sinon label) conservé pour compat dashboard."""
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT s.run_id,
                   COALESCE(s.args->>'doctrine', s.args->>'label') AS slug,
                   s.args->>'label'    AS label,
                   s.args->>'doctrine' AS doctrine,
                   s.sub,
                   s.created_at      AS started_at,
                   f.created_at      AS finished_at,
                   f.args->>'outcome' AS outcome,
                   COALESCE(c.n_calls, 0) AS n_calls
            FROM tool_calls s
            LEFT JOIN LATERAL (
                SELECT created_at, args FROM tool_calls
                WHERE tool = 'run_finish' AND args->>'run_id' = s.run_id
                ORDER BY created_at DESC LIMIT 1
            ) f ON TRUE
            LEFT JOIN (
                SELECT run_id, count(*) AS n_calls FROM tool_calls
                WHERE run_id IS NOT NULL GROUP BY run_id
            ) c ON c.run_id = s.run_id
            WHERE s.tool = 'run_start' AND s.run_id IS NOT NULL
            ORDER BY s.created_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()]


def get_run(run_id: str) -> list[dict]:
    """Timeline d'un déroulé : tous les appels du run, dans l'ordre."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT created_at, tool, args, ok, error, duration_ms
            FROM tool_calls WHERE run_id = %s ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()]


def aggregate_gaps(days: int = 30) -> list[dict]:
    """Manques agrégés (cas d'usage non couverts) — backlog produit dérivé."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT kind, target AS intent, count(*) AS n, max(created_at) AS last_at
            FROM usage_signals
            WHERE signal = 'gap' AND created_at > NOW() - make_interval(days => %s)
            GROUP BY kind, target ORDER BY n DESC, last_at DESC
            """,
            (int(days),),
        ).fetchall()]


def aggregate_tool_feedback(days: int = 30) -> list[dict]:
    """Qualité d'outil agrégée : feedback par (outil, kind)."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT target AS tool, kind, count(*) AS n, max(created_at) AS last_at
            FROM usage_signals
            WHERE signal = 'tool_feedback' AND created_at > NOW() - make_interval(days => %s)
            GROUP BY target, kind ORDER BY n DESC, last_at DESC
            """,
            (int(days),),
        ).fetchall()]


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
        clauses.append("l.tool = %s")
        params.append(tool_name)
    if errors_only:
        clauses.append("l.ok = FALSE")
    if since_days is not None:
        clauses.append("l.created_at >= NOW() - make_interval(days => %s)")
        params.append(int(since_days))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        # Alias tool_name/called_at : compat avec l'UI admin existante.
        rows = conn.execute(
            f"""
            SELECT l.id, l.sub, u.email, u.name, l.tool AS tool_name, l.created_at AS called_at,
                   l.duration_ms, l.ok, l.error
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            {where}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def list_tool_calls_for_org(
    org_id: int, since: Optional[str] = None, until: Optional[str] = None,
    limit: int = 1000,
) -> list[dict]:
    """Journal d'audit org-scopé (export #67) : appels émis **sous** `org_id`
    (colonne `tool_calls.org_id`, stampée par le seam `current_org` au moment de
    l'appel — scope EXACT, pas l'appartenance), récent d'abord, fenêtre
    `[since, until]` (ISO timestamptz, bornes incluses). JAMAIS d'args ni de secret
    (garantie calllog). ⚠ Les appels antérieurs à la colonne (`org_id` NULL)
    n'apparaissent dans aucun export org — non reconstructibles a posteriori."""
    limit = max(1, min(int(limit), 5000))
    clauses = ["l.org_id = %s"]
    params: list[Any] = [int(org_id)]
    if since:
        clauses.append("l.created_at >= %s::timestamptz"); params.append(since)
    if until:
        clauses.append("l.created_at <= %s::timestamptz"); params.append(until)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT l.id, l.created_at, l.sub, u.email, l.tool, l.ok, l.error, l.duration_ms
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE {" AND ".join(clauses)}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def instruction_usage(
    subs: list[str], tool: str, slug: Optional[str], days: int = 30
) -> dict:
    """Usage d'une doctrine dérivé de `tool_calls` (ADR 0014, « doctrine = process
    = log d'usage ») : combien de fois elle a été chargée par l'agent, par qui,
    et la distribution journalière sur `days` jours.

    `tool` = `oto_get_doctrine` (slug=None pour la base, sinon filtré par
    `args->>'slug'` pour une skill). Scopé aux `subs` (membres de
    l'org). Lecture pure ; renvoie {count, callers, daily{date:str -> n}}.
    """
    if not subs:
        return {"count": 0, "callers": [], "daily": {}}
    days = max(1, min(int(days), 365))
    slug_clause = " AND l.args->>'slug' = %s" if slug is not None else ""
    base_params: list[Any] = [subs, tool]
    if slug is not None:
        base_params.append(slug)
    with _connect() as conn:
        callers = conn.execute(
            f"""
            SELECT u.email, COUNT(*) AS n
            FROM tool_calls l LEFT JOIN users u ON u.sub = l.sub
            WHERE l.sub = ANY(%s) AND l.tool = %s{slug_clause} AND l.ok
            GROUP BY u.email ORDER BY n DESC
            """,
            tuple(base_params),
        ).fetchall()
        daily = conn.execute(
            f"""
            SELECT (l.created_at AT TIME ZONE 'UTC')::date AS d, COUNT(*) AS n
            FROM tool_calls l
            WHERE l.sub = ANY(%s) AND l.tool = %s{slug_clause} AND l.ok
              AND l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY d
            """,
            tuple(base_params + [days]),
        ).fetchall()
    return {
        "count": sum(int(r["n"]) for r in callers),
        "callers": [r["email"] for r in callers if r["email"]],
        "daily": {str(r["d"]): int(r["n"]) for r in daily},
    }


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
            FROM tool_calls
            WHERE created_at >= NOW() - make_interval(days => %s)
            """,
            (since_days,),
        ).fetchone() or {}
        by_tool = conn.execute(
            """
            SELECT tool AS tool_name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   ROUND(AVG(duration_ms))::int AS avg_ms
            FROM tool_calls
            WHERE created_at >= NOW() - make_interval(days => %s)
            GROUP BY tool
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
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY l.sub, u.email, u.name
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        by_day = conn.execute(
            """
            SELECT to_char(created_at::date, 'YYYY-MM-DD') AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors
            FROM tool_calls
            WHERE created_at >= NOW() - make_interval(days => %s)
            GROUP BY created_at::date
            ORDER BY created_at::date
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


def prune_tool_calls(keep_days: int = 30) -> int:
    """Retire les lignes de journal plus vieilles que `keep_days`. Borne la
    volumétrie (appelé au boot dans init_db). Retourne le nombre de lignes
    supprimées."""
    keep_days = max(1, int(keep_days))
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM tool_calls WHERE created_at < NOW() - make_interval(days => %s)",
            (keep_days,),
        )
        return cur.rowcount or 0


# --- file d'envoi d'email différé (scheduled_emails) ------------------------

_SCHED_MAX_ATTEMPTS = 3


def enqueue_scheduled_email(*, org_id: Optional[int], created_by: Optional[str],
                            to_email: str, subject: str, body_html: str,
                            from_email: Optional[str], from_name: Optional[str],
                            reply_to: Optional[str], transport: str,
                            scheduled_at: datetime) -> int:
    """Met un email en file pour envoi différé (HTML déjà rendu, autz déjà vérifiée).
    `scheduled_at` doit être un datetime aware (UTC). Retourne l'id."""
    with _connect() as conn:
        row = conn.execute(
            """INSERT INTO scheduled_emails
                 (org_id, created_by, to_email, subject, body_html, from_email,
                  from_name, reply_to, transport, scheduled_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (org_id, created_by, to_email, subject, body_html, from_email,
             from_name, reply_to, transport, scheduled_at),
        ).fetchone()
        return int(row["id"])


def claim_due_scheduled_emails(limit: int = 50) -> list[dict]:
    """Réclame atomiquement les emails dus (pending & scheduled_at <= now), en
    incrémentant `attempts` (claim). `FOR UPDATE SKIP LOCKED` = sûr même si deux
    boucles tournaient. Retourne les lignes à envoyer."""
    with _connect() as conn:
        rows = conn.execute(
            """UPDATE scheduled_emails SET attempts = attempts + 1
               WHERE id IN (
                   SELECT id FROM scheduled_emails
                   WHERE status = 'pending' AND scheduled_at <= NOW()
                   ORDER BY scheduled_at ASC
                   FOR UPDATE SKIP LOCKED
                   LIMIT %s)
               RETURNING id, org_id, to_email, subject, body_html, from_email,
                         from_name, reply_to, transport, attempts""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_scheduled_sent(email_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE scheduled_emails SET status = 'sent', sent_at = NOW(), error = NULL "
            "WHERE id = %s", (email_id,),
        )


def mark_scheduled_failed(email_id: int, error: str) -> None:
    """Échec d'une tentative : repasse en `pending` pour réessayer au prochain tick
    tant que `attempts < _SCHED_MAX_ATTEMPTS` ; sinon fige en `failed`."""
    with _connect() as conn:
        conn.execute(
            """UPDATE scheduled_emails
               SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                   error = %s
               WHERE id = %s""",
            (_SCHED_MAX_ATTEMPTS, error[:500], email_id),
        )


def list_scheduled_emails(org_id: int, status: str = "pending", limit: int = 100) -> list[dict]:
    """Emails programmés d'une org (par statut ; 'all' = tous). Sans le HTML."""
    where = "org_id = %s"
    params: list = [org_id]
    if status and status != "all":
        where += " AND status = %s"
        params.append(status)
    params.append(max(1, int(limit)))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, to_email, subject, from_email, from_name, transport, status,
                       scheduled_at, attempts, sent_at, error, created_at, created_by
                FROM scheduled_emails WHERE {where}
                ORDER BY scheduled_at ASC LIMIT %s""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def cancel_scheduled_email(org_id: int, email_id: int) -> bool:
    """Annule un email encore `pending` de l'org. False si introuvable / déjà parti."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE scheduled_emails SET status = 'cancelled' "
            "WHERE id = %s AND org_id = %s AND status = 'pending'",
            (email_id, org_id),
        )
        return (cur.rowcount or 0) > 0


# --- per-user disabled tools (scopés par org, ADR 0015 ; org_id=0 = perso) --
# Profil = (sub, org_id). org_id=0 = identité perso/globale (aucune org active) ;
# >0 = profil de cette org. Les méta-tools/REST/middleware passent l'org active.

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


# --- per-user enabled overrides (pour les tools masqués par défaut) ---------


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


# --- per-user presets (scopés par org, ADR 0015) ---------------------------

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
    from .. import credentials_store
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
    from .. import credentials_store
    account = google_email or ""
    credentials_store.update_meta(
        "user", sub, GOOGLE, account,
        {"access_token": access_token, "expires_at": expires_at})


def get_google_oauth(sub: str, account: Optional[str] = None) -> Optional[dict]:
    """Renvoie un compte Google du user depuis le COFFRE (déchiffre le
    refresh_token). `account` (email) cible un compte ; None = le défaut
    (meta.is_default), à défaut le plus ancien (granted_at)."""
    from .. import credentials_store
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
    from .. import credentials_store
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
    from .. import credentials_store
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
    from .. import credentials_store
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

def create_datastore_namespace(owner_type: str, owner_id: str, namespace: str) -> int:
    """Crée un namespace possédé par `(owner_type, owner_id)` (ADR 0030). `owner_type`
    ∈ {user, org, group} ; `owner_id` = sub | org.id::text | group.id::text. Lève si
    le même propriétaire a déjà ce nom."""
    if owner_type == "user":
        upsert_user(owner_id)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO user_datastores (owner_type, owner_id, namespace) "
                "VALUES (%s, %s, %s) RETURNING id",
                (owner_type, owner_id, namespace),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"namespace `{namespace}` existe déjà") from e
        return int(row["id"])


def get_datastore_namespace(owner_type: str, owner_id: str, namespace: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, owner_type, owner_id, namespace, created_at FROM user_datastores "
            "WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
            (owner_type, owner_id, namespace),
        ).fetchone()
        return dict(row) if row else None


def get_datastore_namespace_by_id(ns_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, owner_type, owner_id, namespace, created_at FROM user_datastores WHERE id = %s",
            (ns_id,),
        ).fetchone()
        return dict(row) if row else None


def list_datastore_namespaces_for_owners(owners: list[tuple[str, str]]) -> list[dict]:
    """Namespaces possédés par l'un des `(owner_type, owner_id)` fournis."""
    if not owners:
        return []
    otypes = [o[0] for o in owners]
    oids = [o[1] for o in owners]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace, d.created_at "
            "FROM user_datastores d "
            "JOIN unnest(%s::text[], %s::text[]) AS o(t, i) "
            "  ON d.owner_type = o.t AND d.owner_id = o.i "
            "ORDER BY d.namespace",
            (otypes, oids),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Projets (couche d'organisation, owned resource ADR 0030) ----------------
_PROJECT_COLS = ("id, owner_type, owner_id, name, brief_md, created_by, "
                 "archived_at, created_at, updated_at")


def create_project(owner_type: str, owner_id: str, name: str,
                   brief_md: str = "", created_by: Optional[str] = None) -> int:
    """Crée un projet possédé par `(owner_type, owner_id)` (ADR 0030). owner_id = sub
    (perso) | org.id::text | group.id::text."""
    if owner_type == "user":
        upsert_user(owner_id)
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO projects (owner_type, owner_id, name, brief_md, created_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (owner_type, owner_id, name, brief_md, created_by),
        ).fetchone()
        return int(row["id"])


def get_project_by_id(project_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_PROJECT_COLS} FROM projects WHERE id = %s", (project_id,),
        ).fetchone()
        return dict(row) if row else None


def list_projects_for_owners(owners: list[tuple[str, str]], *,
                             include_archived: bool = False) -> list[dict]:
    """Projets possédés par l'un des `(owner_type, owner_id)` (perso + orgs/groupes)."""
    if not owners:
        return []
    otypes = [o[0] for o in owners]
    oids = [o[1] for o in owners]
    sql = (f"SELECT {_PROJECT_COLS} FROM projects p "
           "JOIN unnest(%s::text[], %s::text[]) AS o(t, i) "
           "  ON p.owner_type = o.t AND p.owner_id = o.i ")
    if not include_archived:
        sql += "WHERE p.archived_at IS NULL "
    sql += "ORDER BY p.updated_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql, (otypes, oids)).fetchall()
        return [dict(r) for r in rows]


def list_all_projects(*, include_archived: bool = False) -> list[dict]:
    """Tous les projets (vue opérateur plateforme — gouvernance, pas de contenu)."""
    sql = f"SELECT {_PROJECT_COLS} FROM projects "
    if not include_archived:
        sql += "WHERE archived_at IS NULL "
    sql += "ORDER BY updated_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def update_project(project_id: int, *, name: Optional[str] = None,
                   brief_md: Optional[str] = None) -> None:
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if brief_md is not None:
        sets.append("brief_md = %s")
        params.append(brief_md)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    params.append(project_id)
    with _connect() as conn:
        conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = %s", tuple(params))


def archive_project(project_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE projects SET archived_at = NOW(), updated_at = NOW() "
                     "WHERE id = %s", (project_id,))


def reparent_project(project_id: int, new_owner_type: str, new_owner_id: str) -> None:
    if new_owner_type == "user":
        upsert_user(new_owner_id)
    with _connect() as conn:
        conn.execute("UPDATE projects SET owner_type = %s, owner_id = %s, updated_at = NOW() "
                     "WHERE id = %s", (new_owner_type, new_owner_id, project_id))


def add_project_link(project_id: int, target_type: str, target_ref: str,
                     label: Optional[str] = None, role: Optional[str] = None,
                     config: Optional[dict] = None) -> None:
    """Lie une entité (tableau/procédure/connecteur/base) au projet. Idempotent :
    re-lier met à jour le label ; le `role` et le `config` (surcharge contextuelle
    préfaite, ADR 0032 §4) ne sont écrasés que s'ils sont fournis (un re-link pour
    changer le seul label ne perd ni la description de rôle ni la config déjà posées).
    `config` absent à la création → `{}`."""
    cfg = json.dumps(config) if config is not None else None
    with _connect() as conn:
        conn.execute(
            "INSERT INTO project_links (project_id, target_type, target_ref, label, role, config) "
            "VALUES (%s, %s, %s, %s, %s, COALESCE(%s::jsonb, '{}'::jsonb)) "
            "ON CONFLICT (project_id, target_type, target_ref) DO UPDATE SET "
            "label = EXCLUDED.label, role = COALESCE(EXCLUDED.role, project_links.role), "
            "config = COALESCE(%s::jsonb, project_links.config)",
            (project_id, target_type, target_ref, label, role, cfg, cfg),
        )
        conn.execute("UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))


def remove_project_link(project_id: int, target_type: str, target_ref: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM project_links WHERE project_id = %s AND target_type = %s AND target_ref = %s",
            (project_id, target_type, target_ref),
        )
        return cur.rowcount


def list_project_links(project_id: int) -> list[dict]:
    """Liens du projet, avec `role` et `cross_project` DÉRIVÉ (ADR 0032 §2) : True si
    le même (target_type, target_ref) est lié par un AUTRE projet → l'agent sait qu'une
    modif de l'entité retombe ailleurs (s'abstenir d'un changement brutal / demander)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pl.target_type, pl.target_ref, pl.label, pl.role, pl.config, pl.created_at, "
            "       EXISTS(SELECT 1 FROM project_links o "
            "              WHERE o.target_type = pl.target_type "
            "                AND o.target_ref = pl.target_ref "
            "                AND o.project_id <> pl.project_id) AS cross_project "
            "FROM project_links pl WHERE pl.project_id = %s "
            "ORDER BY pl.target_type, pl.label NULLS LAST, pl.target_ref",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Docs (pages markdown arborescentes d'un projet, incrément 3) -------------
_DOC_COLS = ("id, project_id, parent_id, title, body_md, kind, created_by, "
             "created_at, updated_at")


def create_doc(project_id: int, title: str, *, parent_id: Optional[int] = None,
               body_md: str = "", kind: str = "doc", created_by: Optional[str] = None) -> int:
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO docs (project_id, parent_id, title, body_md, kind, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (project_id, parent_id, title, body_md, kind, created_by),
        ).fetchone()
        conn.execute("UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))
        return int(row["id"])


def get_doc_by_id(doc_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {_DOC_COLS} FROM docs WHERE id = %s", (doc_id,)).fetchone()
        return dict(row) if row else None


def list_docs_for_project(project_id: int) -> list[dict]:
    """Toutes les pages du projet (l'UI/agent reconstruit l'arbre via parent_id)."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_DOC_COLS} FROM docs WHERE project_id = %s "
            "ORDER BY parent_id NULLS FIRST, title", (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_doc(doc_id: int, *, title: Optional[str] = None,
               body_md: Optional[str] = None, kind: Optional[str] = None) -> None:
    sets: list[str] = []
    params: list = []
    if title is not None:
        sets.append("title = %s")
        params.append(title)
    if body_md is not None:
        sets.append("body_md = %s")
        params.append(body_md)
    if kind is not None:
        sets.append("kind = %s")
        params.append(kind)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    params.append(doc_id)
    with _connect() as conn:
        conn.execute(f"UPDATE docs SET {', '.join(sets)} WHERE id = %s", tuple(params))


def delete_doc(doc_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM docs WHERE id = %s", (doc_id,))


def move_doc(doc_id: int, new_parent_id: Optional[int]) -> None:
    with _connect() as conn:
        conn.execute("UPDATE docs SET parent_id = %s, updated_at = NOW() WHERE id = %s",
                     (new_parent_id, doc_id))


# --- Journal d'activité du projet (incrément 5) ------------------------------
def log_project_activity(project_id: int, sub: Optional[str], action: str,
                         detail: Optional[str] = None) -> None:
    """Best-effort : ne jamais faire échouer la mutation principale sur un log raté."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO project_activity (project_id, sub, action, detail) "
                "VALUES (%s, %s, %s, %s)", (project_id, sub, action, detail),
            )
    except Exception:
        logger.warning("log_project_activity échoué (project=%s action=%s)", project_id, action)


def list_project_activity(project_id: int, limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, action, detail, created_at FROM project_activity "
            "WHERE project_id = %s ORDER BY created_at DESC LIMIT %s",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Fichiers bruts d'un projet (carte « Autre document », ADR 0032 §3) -------
_PFILE_COLS = ("id, project_id, s3_key, filename, mime, size_bytes, title, "
               "description, summary, created_by, created_at")


def add_project_file(project_id: int, s3_key: str, filename: str, *,
                     mime: Optional[str] = None, size_bytes: Optional[int] = None,
                     title: Optional[str] = None, description: Optional[str] = None,
                     created_by: Optional[str] = None) -> dict:
    """Enregistre un fichier brut (déjà uploadé en S3) attaché au projet. Le blob
    durable vit dans Object Storage (`s3_key`) ; cette ligne porte sa métadonnée."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO project_files (project_id, s3_key, filename, mime, "
            "size_bytes, title, description, created_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_PFILE_COLS}",
            (project_id, s3_key, filename, mime, size_bytes, title, description, created_by),
        ).fetchone()
        conn.execute("UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))
        return dict(row)


def list_project_files(project_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_PFILE_COLS} FROM project_files WHERE project_id = %s "
            "ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_project_file(file_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_PFILE_COLS} FROM project_files WHERE id = %s", (file_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_project_file(file_id: int) -> Optional[dict]:
    """Supprime la ligne et renvoie la `s3_key` à purger (ou None si inconnu)."""
    with _connect() as conn:
        row = conn.execute(
            "DELETE FROM project_files WHERE id = %s RETURNING project_id, s3_key",
            (file_id,),
        ).fetchone()
        return dict(row) if row else None


def resolve_datastore_ns(
    namespace: str, *, sub: str, org_ids: list[int], group_ids: list[int],
) -> Optional[dict]:
    """Résout un namespace VISIBLE par l'acteur, par NOM, parmi : possédé en perso,
    possédé par une de ses orgs, ou accordé (grant user/org/group). Priorité
    perso > org > grant. Retourne la ligne `user_datastores` (avec `id`) ou None.
    La décision read/write fine est ensuite faite par `ownership.can_access` sur l'id."""
    org_txt = [str(o) for o in org_ids]
    grp_txt = [str(g) for g in group_ids]
    with _connect() as conn:
        row = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace, d.created_at "
            "FROM user_datastores d "
            "WHERE d.namespace = %(ns)s AND ("
            "     (d.owner_type = 'user' AND d.owner_id = %(sub)s)"
            "  OR (d.owner_type = 'org'  AND d.owner_id = ANY(%(org)s))"
            "  OR EXISTS ("
            "       SELECT 1 FROM resource_grants g"
            "        WHERE g.resource_type = 'datastore_namespace' AND g.resource_id = d.id::text"
            "          AND ( (g.principal_type = 'user'  AND g.principal_id = %(sub)s)"
            "             OR (g.principal_type = 'org'   AND g.principal_id = ANY(%(org)s))"
            "             OR (g.principal_type = 'group' AND g.principal_id = ANY(%(grp)s)) ))"
            ") "
            "ORDER BY CASE WHEN d.owner_type='user' AND d.owner_id=%(sub)s THEN 0 "
            "              WHEN d.owner_type='org' THEN 1 ELSE 2 END "
            "LIMIT 1",
            {"ns": namespace, "sub": sub, "org": org_txt, "grp": grp_txt},
        ).fetchone()
        return dict(row) if row else None


def list_datastore_namespaces_granted_to(
    sub: str, org_ids: list[int], group_ids: list[int],
) -> list[dict]:
    """Namespaces accordés à l'acteur via `resource_grants` (principal user/org/group),
    avec la permission gagnante. Exclut ceux possédés en perso (gérés à part)."""
    org_txt = [str(o) for o in org_ids]
    grp_txt = [str(g) for g in group_ids]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace, d.created_at, "
            "       max(g.permission) AS permission "
            "FROM resource_grants g "
            "JOIN user_datastores d ON d.id::text = g.resource_id "
            "WHERE g.resource_type = 'datastore_namespace' AND ("
            "     (g.principal_type = 'user'  AND g.principal_id = %(sub)s)"
            "  OR (g.principal_type = 'org'   AND g.principal_id = ANY(%(org)s))"
            "  OR (g.principal_type = 'group' AND g.principal_id = ANY(%(grp)s)) ) "
            "AND NOT (d.owner_type = 'user' AND d.owner_id = %(sub)s) "
            "GROUP BY d.id, d.owner_type, d.owner_id, d.namespace, d.created_at "
            "ORDER BY d.namespace",
            {"sub": sub, "org": org_txt, "grp": grp_txt},
        ).fetchall()
        return [dict(r) for r in rows]


def rename_datastore_namespace_by_id(ns_id: int, new: str) -> bool:
    """Renomme un namespace par id (l'id BIGSERIAL est conservé → URL/deeplink/grants
    stables ; les grants sont keyés par id, donc rien à propager). Lève si le même
    propriétaire a déjà ce nom, ou si l'id est introuvable."""
    new = (new or "").strip()
    if not new:
        raise ValueError("nouveau nom de namespace requis")
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "SELECT owner_type, owner_id, namespace FROM user_datastores WHERE id = %s FOR UPDATE",
                (ns_id,),
            ).fetchone()
            if not cur:
                raise ValueError("namespace introuvable")
            if cur["namespace"] == new:
                return True
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
                (cur["owner_type"], cur["owner_id"], new),
            ).fetchone():
                raise ValueError(f"un namespace `{new}` existe déjà")
            conn.execute(
                "UPDATE user_datastores SET namespace = %s WHERE id = %s", (new, ns_id),
            )
    return True


def delete_datastore_namespace_by_id(ns_id: int) -> bool:
    """Supprime un namespace par id (CASCADE sur `datastore_rows`) + ses grants
    (`resource_grants` n'a pas de FK car `resource_id` est générique)."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM resource_grants WHERE resource_type = 'datastore_namespace' AND resource_id = %s",
                (str(ns_id),),
            )
            cur = conn.execute("DELETE FROM user_datastores WHERE id = %s", (ns_id,))
        return cur.rowcount > 0


def reparent_datastore_namespace(ns_id: int, new_owner_type: str, new_owner_id: str) -> None:
    """Re-parente un namespace vers un nouveau propriétaire (cœur du transfert).
    Lève si le destinataire possède déjà un namespace de ce nom."""
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT namespace FROM user_datastores WHERE id = %s FOR UPDATE", (ns_id,),
            ).fetchone()
            if not row:
                raise ValueError("namespace introuvable")
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
                (new_owner_type, new_owner_id, row["namespace"]),
            ).fetchone():
                raise ValueError(f"le destinataire possède déjà un namespace `{row['namespace']}`")
            conn.execute(
                "UPDATE user_datastores SET owner_type = %s, owner_id = %s WHERE id = %s",
                (new_owner_type, new_owner_id, ns_id),
            )


def list_all_datastore_namespaces() -> list[dict]:
    """Tous les namespaces, toutes propriétés confondues — pour l'object-browser
    PLATEFORME (gate super_admin/platform_admin côté capacité)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, owner_type, owner_id, namespace, created_at "
            "FROM user_datastores ORDER BY owner_type, owner_id, namespace",
        ).fetchall()
        return [dict(r) for r in rows]


def count_datastore_rows_for_ns(ns_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s", (ns_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


# --- Resource grants (primitive de partage générique, ADR 0030) --------------

def grant_resource(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
    permission: str = "write", granted_by: Optional[str] = None,
) -> None:
    """Accorde (ou met à jour) une permission à un principal sur une ressource.
    Idempotent : ON CONFLICT met à jour la permission."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO resource_grants "
            "(resource_type, resource_id, principal_type, principal_id, permission, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (resource_type, resource_id, principal_type, principal_id) "
            "DO UPDATE SET permission = EXCLUDED.permission, granted_by = EXCLUDED.granted_by",
            (resource_type, resource_id, principal_type, principal_id, permission, granted_by),
        )


def revoke_resource_grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM resource_grants WHERE resource_type = %s AND resource_id = %s "
            "AND principal_type = %s AND principal_id = %s",
            (resource_type, resource_id, principal_type, principal_id),
        )
        return cur.rowcount > 0


def get_resource_grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT permission FROM resource_grants WHERE resource_type = %s AND resource_id = %s "
            "AND principal_type = %s AND principal_id = %s",
            (resource_type, resource_id, principal_type, principal_id),
        ).fetchone()
        return dict(row) if row else None


def list_resource_grants(resource_type: str, resource_id: str) -> list[dict]:
    """Bénéficiaires d'une ressource (principal + permission + email si user), pour
    l'UI de gestion du partage."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.principal_type, g.principal_id, g.permission, g.granted_at, u.email "
            "FROM resource_grants g "
            "LEFT JOIN users u ON g.principal_type = 'user' AND u.sub = g.principal_id "
            "WHERE g.resource_type = %s AND g.resource_id = %s "
            "ORDER BY g.granted_at",
            (resource_type, resource_id),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Datastore rows (substrat PG natif, ADR 0016) ---------------------------

def datastore_insert_row(ns_id: int, row_id: str, data: dict,
                         created_at: Optional[str] = None,
                         updated_at: Optional[str] = None) -> dict:
    """Insère une row. `created_at`/`updated_at` optionnels (override pour le
    backfill ; sinon NOW())."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at) "
            "VALUES (%s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()), COALESCE(%s::timestamptz, NOW())) "
            "RETURNING row_id, created_at, updated_at, data",
            (ns_id, row_id, json.dumps(data), created_at, updated_at),
        ).fetchone()
        return dict(row)


def datastore_upsert_row(ns_id: int, row_id: str, data: dict) -> tuple[dict, bool]:
    """Insère OU met à jour une row par sa clé `(ns_id, row_id)`. Idempotent :
    re-poser le même `row_id` remplace `data` au lieu de dupliquer (sert la
    dédup par clé stable, ex. urn LinkedIn). Renvoie `(row, inserted)` où
    `inserted` est True si la row n'existait pas (ON CONFLICT non déclenché)."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at) "
            "VALUES (%s, %s, %s::jsonb, NOW(), NOW()) "
            "ON CONFLICT (ns_id, row_id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW() "
            "RETURNING row_id, created_at, updated_at, data, (xmax = 0) AS inserted",
            (ns_id, row_id, json.dumps(data)),
        ).fetchone()
        inserted = bool(row.pop("inserted"))
        return dict(row), inserted


def datastore_get_row(ns_id: int, row_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT row_id, created_at, updated_at, data FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


# Filtres par colonne (vue tableau dashboard, oto-dashboard#18). Chaque filtre =
# {field, op, value}. Le champ est TOUJOURS paramétré (`data ->> %s`) et l'op tiré
# d'une whitelist → fragment SQL fixe, zéro interpolation de valeur = pas d'injection.
_DS_FILTER_OPS = {"contains", "eq", "ne", "in", "gt", "gte", "lt", "lte", "empty", "not_empty"}
_DS_CMP_SQL = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_DS_NUM_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")  # numérique strict (pas de nan/1e5)
_DS_MAX_FILTERS = 30


def _ds_filter_clauses(filters: Optional[list]) -> tuple[list[str], list]:
    """Construit les fragments WHERE (combinés en AND) pour des filtres par colonne
    JSONB. Champ paramétré + op whitelisté → pas d'injection. Les comparaisons
    ordonnées (`gt/gte/lt/lte`) sont numériques si la valeur EST numérique (cast
    gardé `::numeric`, les rows non numériques sont écartées), sinon textuelles
    (l'ISO `YYYY-MM-DD` se compare correctement en lexicographique). Lève
    `ValueError` sur un filtre malformé (→ 400 côté route)."""
    clauses: list[str] = []
    params: list = []
    if not filters:
        return clauses, params
    if len(filters) > _DS_MAX_FILTERS:
        raise ValueError("too many filters")
    for f in filters:
        if not isinstance(f, dict):
            raise ValueError("invalid filter")
        field, op, val = f.get("field"), f.get("op"), f.get("value")
        if not isinstance(field, str) or not field or op not in _DS_FILTER_OPS:
            raise ValueError("invalid filter")
        if op == "empty":
            clauses.append("(data ->> %s IS NULL OR data ->> %s = '')")
            params.extend([field, field])
        elif op == "not_empty":
            clauses.append("(data ->> %s IS NOT NULL AND data ->> %s <> '')")
            params.extend([field, field])
        elif op == "in":
            vals = [str(v) for v in (val if isinstance(val, list) else [val])
                    if v is not None and str(v) != ""]
            if not vals:
                continue
            clauses.append("data ->> %s = ANY(%s)")
            params.extend([field, vals])
        elif op == "contains":
            clauses.append("data ->> %s ILIKE %s")
            params.extend([field, f"%{val}%"])
        elif op == "eq":
            clauses.append("data ->> %s = %s")
            params.extend([field, str(val)])
        elif op == "ne":
            clauses.append("(data ->> %s IS DISTINCT FROM %s)")
            params.extend([field, str(val)])
        else:  # gt/gte/lt/lte
            sym = _DS_CMP_SQL[op]
            sval = str(val)
            if _DS_NUM_RE.match(sval):
                clauses.append(
                    "(data ->> %s ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                    f"AND (data ->> %s)::numeric {sym} %s::numeric)")
                params.extend([field, field, sval])
            else:
                clauses.append(f"data ->> %s {sym} %s")
                params.extend([field, sval])
    return clauses, params


def _ds_where(ns_id: int, q: Optional[str], filters: Optional[list]) -> tuple[str, list]:
    """Clause WHERE partagée par list/count (même filtrage → total cohérent)."""
    where = "WHERE ns_id = %s"
    params: list = [ns_id]
    if q:
        where += " AND data::text ILIKE %s"
        params.append(f"%{q}%")
    fclauses, fparams = _ds_filter_clauses(filters)
    for c in fclauses:
        where += f" AND {c}"
    params.extend(fparams)
    return where, params


def datastore_list_rows(ns_id: int, *, offset: int = 0, limit: Optional[int] = None,
                        order_by: Optional[str] = None, order_dir: str = "desc",
                        q: Optional[str] = None, filters: Optional[list] = None) -> list[dict]:
    """Page de rows d'un namespace. `order_by` : `_created_at`/`_updated_at`/`_id`
    (colonnes méta) ou un nom de champ user → `data->>field`. `q` : recherche
    plein-texte sur tout le JSON (`data::text ILIKE`). `filters` : filtres par
    colonne (liste `{field, op, value}`, combinés AND — cf. `_ds_filter_clauses`).
    Tri/pagination/recherche/filtres côté SQL (server-side, ADR 0016). `limit=None`
    = toutes les rows (compat `store.list_rows` / MCP `data_rows`)."""
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    where, params = _ds_where(ns_id, q, filters)
    if order_by in (None, "", "_created_at"):
        order_sql = f"created_at {direction}, row_id {direction}"
    elif order_by == "_updated_at":
        order_sql = f"updated_at {direction}, row_id {direction}"
    elif order_by == "_id":
        order_sql = f"row_id {direction}"
    else:
        order_sql = f"data ->> %s {direction}, row_id {direction}"
        params.append(order_by)  # valeur paramétrée → pas d'injection
    tail = ""
    if limit is not None:
        tail = " LIMIT %s OFFSET %s"
        params.extend([limit, offset])
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, created_at, updated_at, data FROM datastore_rows "
            f"{where} ORDER BY {order_sql}{tail}",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_count_rows(ns_id: int, q: Optional[str] = None,
                         filters: Optional[list] = None) -> int:
    """Nombre total de rows d'un namespace (pour la pagination), filtré par `q` et
    les filtres par colonne — même clause que `datastore_list_rows` → total cohérent
    avec la page affichée."""
    where, params = _ds_where(ns_id, q, filters)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM datastore_rows {where}", tuple(params)
        ).fetchone()
        return int(row["n"]) if row else 0


def datastore_update_row(ns_id: int, row_id: str, data: dict, updated_at: str) -> Optional[dict]:
    """Remplace `data` (le store a déjà fusionné le patch) + `updated_at`."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz "
            "WHERE ns_id = %s AND row_id = %s "
            "RETURNING row_id, created_at, updated_at, data",
            (json.dumps(data), updated_at, ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


def datastore_delete_row(ns_id: int, row_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        )
        return (cur.rowcount or 0) > 0


# --- API tokens (CLI auth) --------------------------------------------------

_TOKEN_PREFIX = "oto_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(sub: str, label: str = "cli", ttl_days: Optional[int] = None) -> str:
    """Génère un token, persiste son hash, renvoie le plaintext une seule fois.

    `ttl_days` : si fourni (>0), le token expire après ce délai et est rejeté
    par `verify_api_token`. None = non-expirant (défaut — token CLI long-lived
    stocké en SOPS). La révocation explicite reste `delete_api_token`.
    """
    upsert_user(sub)
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires = f"NOW() + INTERVAL '{int(ttl_days)} days'" if ttl_days and ttl_days > 0 else "NULL"
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO user_api_tokens (sub, label, token_hash, expires_at) "
            f"VALUES (%s, %s, %s, {expires})",
            (sub, label, _hash_token(token)),
        )
    return token


def verify_api_token(token: str) -> Optional[str]:
    """Renvoie le sub du token, et met à jour last_used_at. None si inconnu ou expiré."""
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None
    h = _hash_token(token)
    with _connect() as conn:
        row = conn.execute(
            "SELECT sub FROM user_api_tokens "
            "WHERE token_hash = %s AND (expires_at IS NULL OR expires_at > NOW())",
            (h,),
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
            "SELECT id, label, created_at, last_used_at, expires_at FROM user_api_tokens WHERE sub = %s ORDER BY created_at DESC",
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


# --- BOAMP (avis de marchés publics, france-opendata#3) ----------------------

_BOAMP_COLS = [
    "idweb", "annee", "objet", "organisme",
    "date_publication", "date_limite_reponse", "date_fin_diffusion",
    "dep_publication", "nature_marche", "type_procedure",
    "type_avis_nature", "type_avis_famille", "statut",
    "descripteurs_libelle", "descripteurs_json", "synthese", "url",
]


def upsert_boamp(rows: list[dict]) -> int:
    """Insère/met à jour des avis BOAMP (clé idweb). Idempotent. Retourne le nb
    de lignes traitées. Conçu pour des batches (ingestion jour-par-jour)."""
    if not rows:
        return 0
    cols = ", ".join(_BOAMP_COLS)
    placeholders = ", ".join(["%s"] * len(_BOAMP_COLS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _BOAMP_COLS if c != "idweb")
    sql = (
        f"INSERT INTO boamp ({cols}, ingested_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT (idweb) DO UPDATE SET {updates}, ingested_at = NOW()"
    )
    data = [tuple(r.get(c) for c in _BOAMP_COLS) for r in rows]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, data)
    return len(data)


def _boamp_row(r: dict) -> dict:
    """Normalise une ligne BOAMP : descripteurs_json (TEXT) → liste `descripteurs`."""
    out = dict(r)
    raw = out.pop("descripteurs_json", None)
    if raw:
        try:
            out["descripteurs"] = json.loads(raw)
        except (ValueError, TypeError):
            pass
    out.pop("ingested_at", None)
    return out


def search_boamp(
    query: Optional[str] = None,
    descripteur: Optional[str] = None,
    departement: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    type_marche: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Recherche d'avis BOAMP (table PG). Filtres AND. Renvoie {results, total_count}."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    clauses, params = ["1=1"], []
    if query:
        clauses.append("objet ILIKE %s"); params.append(f"%{query}%")
    if descripteur:
        clauses.append("descripteurs_libelle ILIKE %s"); params.append(f"%{descripteur}%")
    if departement:
        clauses.append("dep_publication = %s"); params.append(departement)
    if date_from:
        clauses.append("date_publication >= %s"); params.append(date_from)
    if date_to:
        clauses.append("date_publication <= %s"); params.append(date_to)
    if type_marche:
        clauses.append("nature_marche = %s"); params.append(type_marche.upper())
    where = " AND ".join(clauses)
    cols = ", ".join(_BOAMP_COLS)
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM boamp WHERE {where}", tuple(params)
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {cols} FROM boamp WHERE {where} "
            "ORDER BY date_publication DESC NULLS LAST, idweb DESC "
            "LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        ).fetchall()
    return {"results": [_boamp_row(r) for r in rows], "total_count": int(total)}


def get_boamp(idweb: str) -> Optional[dict]:
    """Un avis BOAMP par idweb, ou None."""
    cols = ", ".join(_BOAMP_COLS)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM boamp WHERE idweb = %s LIMIT 1", (idweb,)
        ).fetchone()
    return _boamp_row(row) if row else None


def boamp_info() -> dict:
    """Métadonnées pour healthcheck : nb de lignes + plage de dates."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date_publication) AS dmin, "
            "MAX(date_publication) AS dmax FROM boamp"
        ).fetchone()
    return {"total_rows": int(r["n"]), "date_min": r["dmin"], "date_max": r["dmax"]}


def boamp_last_ingested_epoch() -> Optional[float]:
    """Epoch (s) du dernier upsert BOAMP, ou None si table vide. Sert de garde de
    fraîcheur au rafraîchissement in-process (ne pas recrawler si récent)."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT EXTRACT(EPOCH FROM MAX(ingested_at)) AS e FROM boamp"
        ).fetchone()
    return float(r["e"]) if r and r["e"] is not None else None


# --- ACCO (accords d'entreprise, base nationale des accords collectifs) -------
# Colonnes alignées sur france_opendata.acco.COLUMNS (l'ingestion réutilise le parser).

_ACCO_COLS = [
    "id", "nature", "numero", "siret", "raison_sociale", "code_ape", "code_idcc",
    "secteur", "date_texte", "date_depot", "date_effet", "date_fin", "date_maj",
    "date_diffusion", "conforme_version_integrale", "theme_codes", "themes_libelle",
    "syndicats_libelle", "code_postal", "ville", "titre", "url",
]

# Colonnes triables (whitelist anti-injection : sort_by n'est jamais interpolé brut).
_ACCO_SORT = {
    "date": "date_texte", "date_depot": "date_depot",
    "date_diffusion": "date_diffusion", "date_maj": "date_maj",
}


def upsert_acco(rows: list[dict]) -> int:
    """Insère/met à jour des accords (clé id DILA). Idempotent. Pour batches."""
    if not rows:
        return 0
    cols = ", ".join(_ACCO_COLS)
    placeholders = ", ".join(["%s"] * len(_ACCO_COLS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _ACCO_COLS if c != "id")
    sql = (
        f"INSERT INTO acco ({cols}, ingested_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}, ingested_at = NOW()"
    )
    data = [tuple(r.get(c) for c in _ACCO_COLS) for r in rows]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, data)
    return len(data)


def _acco_row(r: dict) -> dict:
    """Normalise une ligne ACCO : theme_codes (TEXT JSON) → liste `theme_codes`."""
    out = dict(r)
    raw = out.get("theme_codes")
    if raw:
        try:
            out["theme_codes"] = json.loads(raw)
        except (ValueError, TypeError):
            out["theme_codes"] = None
    out.pop("ingested_at", None)
    return out


def search_acco(
    query: Optional[str] = None,
    themes: Optional[list[str]] = None,
    nature: Optional[str] = None,
    siren: Optional[str] = None,
    siret: Optional[str] = None,
    idcc: Optional[str] = None,
    departement: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    latest_per_siret: bool = False,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Recherche d'accords d'entreprise (table PG) — primitive neutre, lignes brutes.

    Filtres AND (sauf `themes` : OR interne). `siren` (9 chiffres) matche TOUS les
    établissements de l'entreprise (ACCO indexe l'accord sous le SIRET déposant, pas
    le siège → toujours préférer `siren` à `siret` pour « cette société a-t-elle un
    accord ? »). `latest_per_siret` réduit à 1 ligne par établissement (l'acte le plus
    récent) AVANT d'appliquer date_from/date_to (→ « dernier accord antérieur à X » =
    contrat dormant). Renvoie {results, total_count}."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    order_col = _ACCO_SORT.get(sort_by, "date_texte")
    order_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    order = f"{order_col} {order_dir} NULLS LAST, id {order_dir}"

    # Filtres « population » (avant réduction par SIRET).
    pop, params = ["1=1"], []
    if query:
        pop.append("titre ILIKE %s"); params.append(f"%{query}%")
    if themes:
        ors = []
        for t in themes:
            ors.append("theme_codes LIKE %s"); params.append(f'%"{t}"%')
        pop.append("(" + " OR ".join(ors) + ")")
    if nature:
        pop.append("nature = %s"); params.append(nature.upper())
    if siren:
        pop.append("LEFT(siret, 9) = %s"); params.append(siren)
    if siret:
        pop.append("siret = %s"); params.append(siret)
    if idcc:
        pop.append("code_idcc = %s"); params.append(idcc)
    if departement:
        pop.append("code_postal LIKE %s"); params.append(f"{departement}%")
    pop_clause = " AND ".join(pop)

    # Filtres de date (sur la ligne retenue → après réduction si latest_per_siret).
    date_conds, date_params = [], []
    if date_from:
        date_conds.append("date_texte >= %s"); date_params.append(date_from)
    if date_to:
        date_conds.append("date_texte <= %s"); date_params.append(date_to)
    date_clause = (" AND " + " AND ".join(date_conds)) if date_conds else ""

    cols = ", ".join(_ACCO_COLS)
    if latest_per_siret:
        inner = (
            f"SELECT {cols}, ROW_NUMBER() OVER "
            "(PARTITION BY siret ORDER BY date_texte DESC NULLS LAST, id DESC) AS rn "
            f"FROM acco WHERE {pop_clause} AND siret IS NOT NULL"
        )
        base = f"SELECT {cols} FROM ({inner}) t WHERE rn = 1{date_clause}"
        qparams = params + date_params
    else:
        base = f"SELECT {cols} FROM acco WHERE {pop_clause}{date_clause}"
        qparams = params + date_params

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({base}) c", tuple(qparams)
        ).fetchone()["n"]
        rows = conn.execute(
            f"{base} ORDER BY {order} LIMIT %s OFFSET %s",
            tuple(qparams) + (limit, offset),
        ).fetchall()
    return {"results": [_acco_row(r) for r in rows], "total_count": int(total)}


def get_acco(id_or_numero: str) -> Optional[dict]:
    """Un accord par son id DILA (ACCOTEXT…) ou son numero (T…), ou None."""
    cols = ", ".join(_ACCO_COLS)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM acco WHERE id = %s OR numero = %s LIMIT 1",
            (id_or_numero, id_or_numero),
        ).fetchone()
    return _acco_row(row) if row else None


def acco_themes() -> list[dict]:
    """Catalogue des thèmes présents (code → libellé + nb d'accords). Découverte."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT code, libelle, COUNT(*) AS n FROM acco a, "
            "  UNNEST("
            "    ARRAY(SELECT json_array_elements_text(a.theme_codes::json)), "
            "    string_to_array(a.themes_libelle, ' | ')"
            "  ) AS t(code, libelle) "
            "WHERE a.theme_codes IS NOT NULL "
            "GROUP BY code, libelle ORDER BY n DESC"
        ).fetchall()
    return [{"code": r["code"], "libelle": r["libelle"], "count": int(r["n"])} for r in rows]


def acco_info() -> dict:
    """Métadonnées healthcheck : nb de lignes + plage de dates."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date_texte) AS dmin, MAX(date_texte) AS dmax FROM acco"
        ).fetchone()
    return {"total_rows": int(r["n"]), "date_min": r["dmin"], "date_max": r["dmax"]}


def acco_last_ingested_epoch() -> Optional[float]:
    """Epoch (s) du dernier upsert ACCO, ou None si table vide."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT EXTRACT(EPOCH FROM MAX(ingested_at)) AS e FROM acco"
        ).fetchone()
    return float(r["e"]) if r and r["e"] is not None else None
