"""Accès DB du palier organization (= périmètre / store serveur).

Domaine isolé du monolithe `db.py` : les tables org (orgs, org_members,
org_entitlements) restent déclarées dans `db._SCHEMA` (DDL centralisée, jouée
par `init_db`), mais leurs requêtes vivent ici. Les credentials d'org vivent
dans le coffre chiffré `connector_credentials` (entity_type='org'), pas dans
une table dédiée. Réutilise les primitives partagées de `db` (`_connect`,
`upsert_user`) plutôt que de les dupliquer.

Consommé par : `access.resolve_api_key`/`status_for` (reads org credential) et
`tools/orgs.py` (meta-tools de gestion). Cf. project_oto_mcp_org_tier.
"""
from __future__ import annotations

import re
from typing import Optional

from . import credentials_store
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

    Lit le coffre chiffré `connector_credentials` (entité 'org', déchiffre).
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


def set_org_secret(org_id: int, provider: str, api_key: str, set_by: Optional[str] = None,
                   meta: Optional[dict] = None) -> None:
    """Pose/rote le secret partagé `provider` de l'org. `provider` validé comme
    org-partageable (byo_org : exclut slack/linkedin, inclut mm org-only) via le
    registre — plus restrictif que KEY_PROVIDERS puisque mm n'est pas keyed.
    `meta` : satellites non-secrets (ex. `base_url` du bridge d'un connecteur
    remote, ADR 0003)."""
    connectors.require_credential("org", provider)
    if not api_key:
        raise ValueError("api_key requise")
    # Coffre chiffré, source unique (entité 'org').
    credentials_store.set_credential(
        "org", str(org_id), provider, api_key, set_by=set_by, meta=meta)


def delete_org_secret(org_id: int, provider: str) -> bool:
    return credentials_store.clear_credential("org", str(org_id), provider)


def list_org_secrets(org_id: int) -> list[dict]:
    """Providers posés sur l'org — SANS l'api_key (jamais exposée via API).
    Lit le coffre (entité 'org'). `base_url` exposé pour les connecteurs
    remote (satellite non-secret dans `meta`)."""
    out: list[dict] = []
    for c in credentials_store.list_credentials("org", str(org_id)):
        entry = {"provider": c["connector"], "set_by": c["set_by"], "set_at": c["set_at"]}
        base_url = (c.get("meta") or {}).get("base_url")
        if base_url:
            entry["base_url"] = base_url
        out.append(entry)
    return out


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


# --- instructions d'org : doctrine de base + skills versionnés ----------------
#
# Modèle unifié servi par get_claude_md() / oto_*_instruction(s). Le slug réservé
# BASE_SLUG ("claude_md") = la doctrine de base (servie d'office) ; les autres =
# des skills chargés à la demande. En clair (prose, hors coffre), lu à l'appel
# (pas de cache). Écriture = incrément de version + snapshot d'historique.

BASE_SLUG = "claude_md"
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def normalize_slug(slug: str) -> str:
    """Slug canonique : minuscules, [a-z0-9_-], séparateurs compactés. '' si vide."""
    return _SLUG_RE.sub("-", (slug or "").strip().lower()).strip("-_")


def _snippet(body: str, query: str, width: int = 200) -> str:
    """Extrait de `body` autour de la 1ʳᵉ occurrence de `query` (pour la recherche)."""
    i = body.lower().find(query.lower())
    if i < 0:
        return body[:width].strip()
    start = max(0, i - width // 3)
    end = min(len(body), i + len(query) + (2 * width) // 3)
    return ("…" if start else "") + body[start:end].strip() + ("…" if end < len(body) else "")


def get_instruction(org_id: int, slug: str, version: Optional[int] = None) -> Optional[dict]:
    """Une instruction (courante, ou une `version` archivée précise). None si absente."""
    slug = normalize_slug(slug)
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT org_id, slug, title, description, body_md, version, set_by, "
                "created_at, updated_at FROM org_instructions "
                "WHERE org_id = %s AND slug = %s",
                (org_id, slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT org_id, slug, title, description, body_md, version, set_by, "
                "created_at FROM org_instruction_revisions "
                "WHERE org_id = %s AND slug = %s AND version = %s",
                (org_id, slug, version),
            ).fetchone()
        return dict(row) if row else None


def list_instructions(org_id: int, include_base: bool = False) -> list[dict]:
    """Métadonnées des instructions (SANS body) = l'index des skills. Exclut la
    doctrine de base sauf `include_base` (surface admin)."""
    where = "org_id = %s" if include_base else "org_id = %s AND slug <> %s"
    params: tuple = (org_id,) if include_base else (org_id, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT slug, title, description, version, updated_at "
            f"FROM org_instructions WHERE {where} ORDER BY slug",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def search_instructions(org_id: int, query: str, include_base: bool = False) -> list[dict]:
    """Recherche substring (title/description/body) dans les instructions de l'org.
    Renvoie les métadonnées + un `snippet` ; le body complet passe par get_instruction."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    base_filter = "" if include_base else "AND slug <> %s "
    head: tuple = (org_id,) if include_base else (org_id, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT slug, title, description, body_md, version, updated_at "
            "FROM org_instructions WHERE org_id = %s " + base_filter +
            "AND (title ILIKE %s OR description ILIKE %s OR body_md ILIKE %s) "
            "ORDER BY (title ILIKE %s) DESC, (description ILIKE %s) DESC, updated_at DESC",
            head + (like, like, like, like, like),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snippet"] = _snippet(d.pop("body_md", "") or "", q)
        out.append(d)
    return out


def set_instruction(org_id: int, slug: str, body_md: str, title: Optional[str] = None,
                    description: Optional[str] = None, set_by: Optional[str] = None) -> int:
    """Crée/met à jour une instruction ; renvoie la NOUVELLE version et archive un
    snapshot. `title`/`description` None = conserver l'existant ('' à la création).
    Sérialisé par (org, slug) via verrou advisory (mirroir add_org_member)."""
    slug = normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if not (body_md or "").strip():
        raise ValueError("body_md requis")
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"oi:{org_id}:{slug}",))
            cur = conn.execute(
                "SELECT version, title, description FROM org_instructions "
                "WHERE org_id = %s AND slug = %s",
                (org_id, slug),
            ).fetchone()
            new_version = (cur["version"] + 1) if cur else 1
            new_title = title if title is not None else (cur["title"] if cur else "")
            new_desc = description if description is not None else (cur["description"] if cur else "")
            conn.execute(
                """
                INSERT INTO org_instructions
                    (org_id, slug, title, description, body_md, version, set_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (org_id, slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, version = EXCLUDED.version,
                    set_by = EXCLUDED.set_by, updated_at = NOW()
                """,
                (org_id, slug, new_title, new_desc, body_md, new_version, set_by),
            )
            conn.execute(
                """
                INSERT INTO org_instruction_revisions
                    (org_id, slug, version, title, description, body_md, set_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (org_id, slug, new_version, new_title, new_desc, body_md, set_by),
            )
            return new_version


def list_instruction_versions(org_id: int, slug: str) -> list[dict]:
    """Historique d'une instruction (métadonnées par version, plus récent d'abord)."""
    slug = normalize_slug(slug)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT version, title, set_by, created_at FROM org_instruction_revisions "
            "WHERE org_id = %s AND slug = %s ORDER BY version DESC",
            (org_id, slug),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_instruction(org_id: int, slug: str) -> bool:
    """Supprime une instruction ET son historique. False si elle n'existait pas."""
    slug = normalize_slug(slug)
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM org_instructions WHERE org_id = %s AND slug = %s", (org_id, slug)
            )
            removed = (cur.rowcount or 0) > 0
            conn.execute(
                "DELETE FROM org_instruction_revisions WHERE org_id = %s AND slug = %s",
                (org_id, slug),
            )
    return removed
