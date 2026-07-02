"""Autorisations de compte connecteur partagé (otomata-private#55).

Le propriétaire d'un compte Unipile accorde à un membre nommé le droit d'OPÉRER
son compte sur un canal (`connector_account_grants`), et le grantee pointe le
compte qu'il opère (`unipile_operated_accounts`). Deux plans distincts : le grant
= le DROIT (deny-by-default, révocable, audité) ; le pointeur = le CHOIX courant,
jamais un droit (revalidé contre les grants vivants à chaque appel).

⚠️ Pas de fail-open ici (≠ RBAC ADR 0025) : ce chemin est le backstop d'identité
— une erreur infra doit lever, pas laisser passer une usurpation.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect


def set_account_grant(owner_sub: str, provider: str, account_id: str,
                      grantee_sub: str, granted_by: str) -> None:
    """Accorde (upsert) à `grantee_sub` le droit d'opérer le compte `provider` de
    `owner_sub`. `account_id` = snapshot d'audit (la résolution relit le live)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO connector_account_grants "
            "(owner_sub, provider, account_id, grantee_sub, granted_by) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (owner_sub, provider, grantee_sub) DO UPDATE SET "
            "account_id = EXCLUDED.account_id, granted_by = EXCLUDED.granted_by, "
            "granted_at = NOW()",
            (owner_sub, provider, account_id, grantee_sub, granted_by),
        )


def clear_account_grant(owner_sub: str, provider: str, grantee_sub: str) -> bool:
    """Révoque le grant. True si une ligne a été supprimée (idempotent sinon)."""
    with _connect() as conn:
        n = conn.execute(
            "DELETE FROM connector_account_grants "
            "WHERE owner_sub = %s AND provider = %s AND grantee_sub = %s",
            (owner_sub, provider, grantee_sub),
        ).rowcount
    return n > 0


def list_account_grants_by_owner(owner_sub: str) -> list[dict]:
    """Grants accordés PAR ce propriétaire (face « qui opère mes comptes »).
    `account_id`/`account_name` = état LIVE du compte (LEFT JOIN — None si le
    canal a été déconnecté depuis, le grant est alors inerte)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.provider, ua.account_id, ua.account_name, "
            "g.grantee_sub, u.email AS grantee_email, u.name AS grantee_name, "
            "g.granted_by, g.granted_at, (ua.account_id IS NOT NULL) AS active "
            "FROM connector_account_grants g "
            "LEFT JOIN users u ON u.sub = g.grantee_sub "
            "LEFT JOIN unipile_accounts ua "
            "  ON ua.sub = g.owner_sub AND ua.provider = g.provider "
            "WHERE g.owner_sub = %s ORDER BY g.provider, g.granted_at",
            (owner_sub,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_account_grants_to(grantee_sub: str) -> list[dict]:
    """Grants reçus PAR ce user (face « comptes que je peux opérer »).
    `active=False` si le owner a déconnecté le canal (grant inerte)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.provider, g.owner_sub, u.email AS owner_email, "
            "u.name AS owner_name, ua.account_id, ua.account_name, "
            "g.granted_at, (ua.account_id IS NOT NULL) AS active "
            "FROM connector_account_grants g "
            "LEFT JOIN users u ON u.sub = g.owner_sub "
            "LEFT JOIN unipile_accounts ua "
            "  ON ua.sub = g.owner_sub AND ua.provider = g.provider "
            "WHERE g.grantee_sub = %s ORDER BY g.provider, g.granted_at",
            (grantee_sub,),
        ).fetchall()
    return [dict(r) for r in rows]


def granted_accounts_for(grantee_sub: str, provider: str) -> dict[str, dict]:
    """LE check dur par appel : comptes que `grantee_sub` est autorisé à opérer
    sur ce canal, `{account_id_live: {owner_sub, owner_email}}`. INNER JOIN sur
    le compte VIVANT du owner ⇒ révocation OU déconnexion = disparition immédiate."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ua.account_id, g.owner_sub, u.email AS owner_email "
            "FROM connector_account_grants g "
            "JOIN unipile_accounts ua "
            "  ON ua.sub = g.owner_sub AND ua.provider = g.provider "
            "LEFT JOIN users u ON u.sub = g.owner_sub "
            "WHERE g.grantee_sub = %s AND g.provider = %s",
            (grantee_sub, provider),
        ).fetchall()
    return {r["account_id"]: {"owner_sub": r["owner_sub"],
                              "owner_email": r["owner_email"]} for r in rows}


def users_share_org(sub_a: str, sub_b: str) -> bool:
    """True si les deux users sont membres d'AU MOINS une org commune (anti-IDOR
    au grant : « même org », pas « l'org active »)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM org_members a JOIN org_members b USING (org_id) "
            "WHERE a.sub = %s AND b.sub = %s LIMIT 1",
            (sub_a, sub_b),
        ).fetchone() is not None


def get_operated_account(sub: str, provider: str) -> Optional[dict]:
    """Pointeur « identité opérée » du user pour ce canal
    (`{account_id, owner_sub}`), ou None (= il opère son propre compte)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT account_id, owner_sub FROM unipile_operated_accounts "
            "WHERE sub = %s AND provider = %s",
            (sub, provider),
        ).fetchone()
    return dict(row) if row else None


def set_operated_account(sub: str, provider: str, account_id: str,
                         owner_sub: str) -> None:
    """Pose (upsert) le pointeur « j'opère ce compte accordé » pour ce canal."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO unipile_operated_accounts (sub, provider, account_id, owner_sub) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (sub, provider) DO UPDATE SET "
            "account_id = EXCLUDED.account_id, owner_sub = EXCLUDED.owner_sub, "
            "selected_at = NOW()",
            (sub, provider, account_id, owner_sub),
        )


def clear_operated_account(sub: str, provider: str) -> None:
    """Retour-à-soi : efface le pointeur du canal (le user opère à nouveau SON compte)."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM unipile_operated_accounts WHERE sub = %s AND provider = %s",
            (sub, provider),
        )


def clear_operated_pointers_to(owner_sub: str, provider: str, grantee_sub: str) -> None:
    """Hygiène au revoke : efface le pointeur du grantee s'il opérait ce compte.
    Best-effort — le backstop (`granted_accounts_for` à chaque appel) ne repose PAS
    dessus, un pointeur orphelin lève une erreur explicite à l'appel suivant."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM unipile_operated_accounts "
            "WHERE sub = %s AND provider = %s AND owner_sub = %s",
            (grantee_sub, provider, owner_sub),
        )
