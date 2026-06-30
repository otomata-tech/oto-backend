"""Utilisateurs : identité, migration tenant Logto, accès plateforme & quota, rôle, avatar, profil onboarding.

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


def set_user_role(sub: str, role: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET role = %s, updated_at = NOW() WHERE sub = %s",
            (role, sub),
        )


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
