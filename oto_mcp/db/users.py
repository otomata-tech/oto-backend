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
        # Réconciliation invitation↔signup : un invité d'org qui s'inscrit (par
        # n'importe quel chemin, pas seulement le lien /invite) voit son invitation
        # d'org en attente honorée par l'email vérifié → il rejoint directement
        # l'org au lieu de rester avec une invitation orpheline. Synchrone (une
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
        # Si l'inscription ne l'a pas déjà rattaché à une org (invitation d'org
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
    # ⚠️ Chaque entrée DOIT exister en DB : la boucle fait des UPDATE nus dans UNE
    # transaction — une table absente fait échouer TOUT le merge (vécu : `user_grants`,
    # droppée par 0044 §F mais restée listée → migrate_sub cassé jusqu'au nettoyage
    # Phase H B1 du 10/07, qui a aussi sorti les reliques datastore `user_datastores.sub`
    # et `datastore_shares` : colonnes mortes, plus rien ne les lit, DROP en B2).
    # données de l'user
    ("usage", "sub"), ("tool_calls", "sub"), ("usage_signals", "sub"),
    ("user_disabled_tools", "sub"), ("user_enabled_tools", "sub"),
    ("org_members", "sub"), ("org_group_members", "sub"),
    ("user_api_tokens", "sub"), ("unipile_accounts", "sub"), ("unipile_pending", "sub"),
    # ressources possédées + grants (ère ownership 0030/0042/0048 — ajoutées Phase H B1 :
    # l'inventaire n'avait jamais suivi, une bascule de tenant orphelinait les ressources
    # user-owned et les grants nominatifs). `owner_id`/`principal_id` mélangent sub et
    # ids numériques d'org/groupe : un sub Logto n'est jamais un entier → l'UPDATE nu
    # `col=old_sub` ne peut toucher que les lignes user.
    ("user_datastores", "owner_id"), ("projects", "owner_id"),
    ("resource_grants", "principal_id"), ("resource_grants", "granted_by"),
    ("guides", "owner_id"),
    # attribution (soft)
    ("projects", "created_by"),
    ("orgs", "created_by"),
    ("org_invitations", "invited_by"), ("org_invitations", "accepted_sub"),
    ("org_groups", "created_by"), ("org_instructions", "set_by"),
    ("org_instruction_revisions", "set_by"), ("doctrine_library", "published_by"),
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
        # 1. fusionner le rôle SANS JAMAIS RÉTROGRADER : on prend le rôle le plus
        #    fort. ⚠️ Le naïf « hérite de l'ancien » downgrade le nouveau si l'ancien
        #    est un stub frais (member) re-fusionné par-dessus un compte établi
        #    (vécu 2026-06-23 : alexis super_admin repassé member).
        new = conn.execute(
            "SELECT role FROM users WHERE sub=%s", (new_sub,)
        ).fetchone() or {}
        conn.execute(
            """UPDATE users SET
                 role = %(role)s,
                 avatar_url = COALESCE(users.avatar_url, %(av)s), updated_at = NOW()
               WHERE sub = %(new)s""",
            {"role": _stronger_role(old["role"], new.get("role")),
             "av": old.get("avatar_url"), "new": new_sub},
        )
        # 2. user_account_profile + user_agent_readme (PK sub) : retirer le frais du new
        #    PUIS repointer l'ancien (garde l'historique). DELETE d'abord → pas de conflit PK.
        conn.execute("DELETE FROM user_account_profile WHERE sub=%s", (new_sub,))
        conn.execute("UPDATE user_account_profile SET sub=%s WHERE sub=%s", (new_sub, old_sub))
        conn.execute("DELETE FROM user_agent_readme WHERE sub=%s", (new_sub,))
        conn.execute("UPDATE user_agent_readme SET sub=%s WHERE sub=%s", (new_sub, old_sub))
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


def set_user_locale(sub: str, locale: str) -> None:
    """Pose la préférence de langue de l'UI dashboard ('en'|'fr').

    La validation de l'énum vit dans la capacité `me.locale.set` (Input pydantic) —
    ici on écrit la valeur telle quelle. Colonne en clair (préférence, pas un secret)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET locale = %s, updated_at = NOW() WHERE sub = %s",
            (locale, sub),
        )


def get_account_profile(sub: str) -> dict:
    """Fiche « situation avec oto » de l'user : {profile, updated_at}.

    Jamais None — un sub sans ligne renvoie l'état par défaut (profile vide).
    Lecture seule (ne crée pas la ligne)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT profile, updated_at FROM user_account_profile WHERE sub = %s",
            (sub,),
        ).fetchone()
    if not row:
        return {"profile": {}, "updated_at": None}
    profile = row["profile"]
    if isinstance(profile, str):  # selon le driver, JSONB peut revenir en texte
        try:
            profile = json.loads(profile)
        except Exception:
            profile = {}
    return {"profile": profile or {}, "updated_at": row["updated_at"]}


def get_user_readme(sub: str) -> dict:
    """Agent README personnel de l'user : {body_md, updated_at}. Jamais None —
    un sub sans ligne renvoie l'état vide. Lecture seule (ne crée pas la ligne)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT body_md, updated_at FROM user_agent_readme WHERE sub = %s",
            (sub,),
        ).fetchone()
    if not row:
        return {"body_md": "", "updated_at": None}
    return {"body_md": row["body_md"] or "", "updated_at": row["updated_at"]}


def set_user_readme(sub: str, body_md: str) -> dict:
    """Pose l'agent README personnel (upsert ; corps vide = README effacé, la ligne
    reste). Renvoie l'état résultant."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_agent_readme (sub, body_md, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (sub) DO UPDATE SET
                body_md = EXCLUDED.body_md,
                updated_at = NOW()
            """,
            (sub, body_md or ""),
        )
    return get_user_readme(sub)


def update_account_profile(sub: str, fields: Optional[dict] = None) -> dict:
    """Met à jour la fiche « situation avec oto » (upsert). `fields` est **shallow-mergé**
    dans le JSONB `profile` (clés existantes écrasées, les autres conservées). Renvoie
    l'état résultant (comme `get_account_profile`)."""
    upsert_user(sub)
    patch = json.dumps(fields or {})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_account_profile (sub, profile, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (sub) DO UPDATE SET
                profile = user_account_profile.profile || EXCLUDED.profile,
                updated_at = NOW()
            """,
            (sub, patch),
        )
    return get_account_profile(sub)
