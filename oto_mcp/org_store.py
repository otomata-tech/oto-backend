"""Accès DB du palier organization (= périmètre / store serveur).

Domaine isolé du monolithe `db.py` : les tables org (orgs, org_members,
org_secrets, org_entitlements) restent déclarées dans `db._SCHEMA` (DDL
centralisée, jouée par `init_db`), mais leurs requêtes vivent ici. Réutilise
les primitives partagées de `db` (`_connect`, `upsert_user`)
plutôt que de les dupliquer.

Consommé par : `access.resolve_api_key`/`status_for` (reads org_secret) et
`tools/orgs.py` (meta-tools de gestion). Cf. project_oto_mcp_org_tier.
"""
from __future__ import annotations

from typing import Optional

from . import credentials_store, crypto
from . import connectors
from .db import _connect, upsert_user

ORG_ROLES = ("org_admin", "org_member")


# --- reads consommés par la résolution de clé (barreau 2) -------------------

def get_active_org(sub: str) -> Optional[int]:
    """org_id de l'organisation active du `sub`, ou None s'il n'en a aucune.

    L'index partiel `org_members_one_active` garantit au plus une ligne active
    par sub ; LIMIT 1 reste défensif (ne jamais supposer exactement une TRUE).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id FROM org_members WHERE sub = %s AND is_active LIMIT 1",
            (sub,),
        ).fetchone()
        return int(row["org_id"]) if row else None


def get_org_secret(org_id: int, provider: str) -> Optional[str]:
    """Clé du secret partagé `provider` possédé par l'org, ou None.

    `provider` validé dans le store (require_keyed). La restriction aux providers
    org-partageables (exclut slack) est portée par la couche access et le
    write-path.

    Cutover (Phase 2/C4) : lit connector_credentials (entité 'org'), non plus la
    table legacy org_secrets (toujours dual-written pour le rollback). Déchiffre
    si nécessaire (Phase 7).
    """
    return credentials_store.get_credential("org", str(org_id), provider)


def has_org_secret(org_id: int, provider: str) -> bool:
    """Présence d'un org_secret SANS le déchiffrer (status_for)."""
    return credentials_store.has_credential("org", str(org_id), provider)


# --- écritures + lectures de gestion (barreau 3, meta-tools platform_admin) --

def create_org(name: str, created_by: Optional[str] = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("nom d'org requis")
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO orgs (name, created_by) VALUES (%s, %s) RETURNING id",
            (name, created_by),
        ).fetchone()
        return int(row["id"])


def get_org(org_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, created_by, created_at FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
        return dict(row) if row else None


def list_all_orgs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_by, created_at FROM orgs ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def add_org_member(org_id: int, sub: str, org_role: str = "org_member") -> None:
    """Ajoute (ou met à jour le rôle d') un membre. Auto-promeut l'org en active
    si c'est la 1ère adhésion du sub.

    Contrairement à set_google_oauth (table sans index unique partiel sur le
    flag, où deux TRUE sont tolérés), org_members a l'index partiel
    `org_members_one_active`. Le calcul make_active=(COUNT==0) est donc une
    lecture-modification-écriture qui, sous READ COMMITTED, casserait sur deux
    1ères adhésions concurrentes du MÊME sub (les deux liraient COUNT=0 →
    deux is_active=TRUE → IntegrityError). On sérialise par sub via un verrou
    advisory transactionnel ; `conn.transaction()` seul ne donne que
    l'atomicité, pas cette sérialisation.
    """
    if org_role not in ORG_ROLES:
        raise ValueError(f"org_role invalide {org_role!r} (attendu: {ORG_ROLES})")
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (sub,))
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM org_members WHERE sub = %s", (sub,)
            ).fetchone()["n"]
            make_active = n == 0
            conn.execute(
                """
                INSERT INTO org_members (org_id, sub, org_role, is_active)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (org_id, sub) DO UPDATE SET org_role = EXCLUDED.org_role
                """,
                (org_id, sub, org_role, make_active),
            )


def remove_org_member(org_id: int, sub: str) -> bool:
    """Retire un membre. Si on retire son org active et qu'il en reste, promeut
    la plus ancienne restante (mirroir delete_google_oauth)."""
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM org_members WHERE org_id = %s AND sub = %s", (org_id, sub)
            )
            removed = (cur.rowcount or 0) > 0
            if removed:
                has_active = conn.execute(
                    "SELECT 1 FROM org_members WHERE sub = %s AND is_active", (sub,)
                ).fetchone()
                if not has_active:
                    conn.execute(
                        """
                        UPDATE org_members SET is_active = TRUE
                         WHERE sub = %s AND org_id = (
                             SELECT org_id FROM org_members
                              WHERE sub = %s ORDER BY joined_at ASC LIMIT 1
                         )
                        """,
                        (sub, sub),
                    )
            return removed


def set_active_org(sub: str, org_id: int) -> bool:
    """Bascule l'org active du sub. False si le sub n'est pas membre de l'org."""
    with _connect() as conn:
        with conn.transaction():
            hit = conn.execute(
                "SELECT 1 FROM org_members WHERE org_id = %s AND sub = %s", (org_id, sub)
            ).fetchone()
            if not hit:
                return False
            conn.execute(
                "UPDATE org_members SET is_active = (org_id = %s) WHERE sub = %s",
                (org_id, sub),
            )
            return True


def list_orgs_for_user(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT m.org_id, o.name, m.org_role, m.is_active, m.joined_at
              FROM org_members m JOIN orgs o ON o.id = m.org_id
             WHERE m.sub = %s ORDER BY m.joined_at ASC
            """,
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_org_members(org_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, org_role, is_active, joined_at FROM org_members "
            "WHERE org_id = %s ORDER BY joined_at",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_org_role(org_id: int, sub: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_role FROM org_members WHERE org_id = %s AND sub = %s",
            (org_id, sub),
        ).fetchone()
        return row["org_role"] if row else None


def set_org_secret(org_id: int, provider: str, api_key: str, set_by: Optional[str] = None) -> None:
    """Pose/rote le secret partagé `provider` de l'org. `provider` validé comme
    org-partageable (byo_org : exclut slack/linkedin, inclut mm org-only) via le
    registre — plus restrictif que KEY_PROVIDERS puisque mm n'est pas keyed."""
    connectors.require_credential("org", provider)
    if not api_key:
        raise ValueError("api_key requise")
    # Dual-write ATOMIQUE (legacy org_secrets + canonique) dans une transaction.
    # Chiffrement ON : on n'écrit plus de plaintext en legacy (cf. set_user_api_key).
    with _connect() as conn:
        with conn.transaction():
            if not crypto.encryption_enabled():
                conn.execute(
                    """
                    INSERT INTO org_secrets (org_id, provider, api_key, set_by)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (org_id, provider) DO UPDATE SET
                        api_key = EXCLUDED.api_key, set_by = EXCLUDED.set_by, set_at = NOW()
                    """,
                    (org_id, provider, api_key, set_by),
                )
            credentials_store.set_credential(
                "org", str(org_id), provider, api_key, set_by=set_by, conn=conn)


def delete_org_secret(org_id: int, provider: str) -> bool:
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM org_secrets WHERE org_id = %s AND provider = %s", (org_id, provider)
            )
            removed = (cur.rowcount or 0) > 0
            credentials_store.clear_credential("org", str(org_id), provider, conn=conn)
    return removed


def list_org_secrets(org_id: int) -> list[dict]:
    """Providers posés sur l'org — SANS l'api_key (jamais exposée via API)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT provider, set_by, set_at FROM org_secrets WHERE org_id = %s ORDER BY provider",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- entitlements : plafond de visibilité plateforme -> org (barreau 4) ------

def list_org_entitled_namespaces(org_id: int) -> list[str]:
    """Namespaces gouvernés débloqués pour les membres de l'org."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace FROM org_entitlements WHERE org_id = %s ORDER BY namespace",
            (org_id,),
        ).fetchall()
        return [r["namespace"] for r in rows]


def grant_org_entitlement(org_id: int, namespace: str, granted_by: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_entitlements (org_id, namespace, granted_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (org_id, namespace) DO UPDATE SET
                granted_at = NOW(), granted_by = EXCLUDED.granted_by
            """,
            (org_id, namespace, granted_by),
        )


def revoke_org_entitlement(org_id: int, namespace: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_entitlements WHERE org_id = %s AND namespace = %s",
            (org_id, namespace),
        )
        return (cur.rowcount or 0) > 0


def list_org_entitlements(org_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace, granted_by, granted_at FROM org_entitlements "
            "WHERE org_id = %s ORDER BY namespace",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]
