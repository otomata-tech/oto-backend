"""Accès DB du sous-palier GROUPE (départements / équipes d'une org, ADR 0012).

Miroir d'`org_store` au grain groupe. Les tables (`org_groups`,
`org_group_members`, `org_group_instructions` + revisions) sont déclarées dans
`db._SCHEMA` ; leurs requêtes vivent ici. Les secrets de groupe vivent dans le
coffre chiffré `connector_credentials` (entity_type='group'), comme ceux d'org.

Un groupe gouverne DEUX ressources par délégation de l'org (décision produit) :
- **doctrine** (org_group_instructions) — servie en complément de celle de l'org ;
- **secrets partagés** (coffre, entity_type='group') — résolus avant ceux de l'org.

Sens unique (ADR 0004) : dépend de `db`/`org_store`/`credentials_store`/
`connectors`, jamais l'inverse. `org_store` n'importe PAS ce module (il manipule
`org_group_members` en SQL direct pour l'invariant org↔groupe → pas de cycle).
"""
from __future__ import annotations

from typing import Optional

from . import connectors, credentials_store
from .db import _connect
from .org_store import BASE_SLUG, _snippet, normalize_slug

GROUP_ROLES = ("group_admin", "group_member")


# --- CRUD groupe ------------------------------------------------------------

def create_group(org_id: int, name: str, description: str = "",
                 created_by: Optional[str] = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("nom de groupe requis")
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO org_groups (org_id, name, description, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (org_id, name, (description or "").strip(), created_by),
        ).fetchone()
        return int(row["id"])


def get_group(group_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, org_id, name, description, created_by, created_at "
            "FROM org_groups WHERE id = %s",
            (group_id,),
        ).fetchone()
        return dict(row) if row else None


def list_groups(org_id: int) -> list[dict]:
    """Tous les groupes d'une org (métadonnées, sans les membres)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id, name, description, created_by, created_at "
            "FROM org_groups WHERE org_id = %s ORDER BY name",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_group(group_id: int, name: Optional[str] = None,
                 description: Optional[str] = None) -> bool:
    """Renomme / re-décrit un groupe. None = conserver le champ. False si absent."""
    sets, params = [], []
    if name is not None:
        n = name.strip()
        if not n:
            raise ValueError("nom de groupe vide")
        sets.append("name = %s")
        params.append(n)
    if description is not None:
        sets.append("description = %s")
        params.append(description.strip())
    if not sets:
        return get_group(group_id) is not None
    params.append(group_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE org_groups SET {', '.join(sets)} WHERE id = %s", tuple(params)
        )
        return (cur.rowcount or 0) > 0


def delete_group(group_id: int) -> bool:
    """Supprime un groupe (cascade : membres, doctrine, revisions). Les secrets
    de groupe (coffre) sont purgés explicitement (hors FK)."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM connector_credentials "
                "WHERE entity_type = 'group' AND entity_id = %s",
                (str(group_id),),
            )
            cur = conn.execute("DELETE FROM org_groups WHERE id = %s", (group_id,))
            return (cur.rowcount or 0) > 0


# --- membres ----------------------------------------------------------------

def add_group_member(group_id: int, sub: str, group_role: str = "group_member") -> None:
    """Ajoute (ou met à jour le rôle d') un membre du groupe. Le caller garantit
    que `sub` est déjà membre de l'org parente (invariant ADR 0012)."""
    if group_role not in GROUP_ROLES:
        raise ValueError(f"group_role invalide {group_role!r} (attendu: {GROUP_ROLES})")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_group_members (group_id, sub, group_role)
            VALUES (%s, %s, %s)
            ON CONFLICT (group_id, sub) DO UPDATE SET group_role = EXCLUDED.group_role
            """,
            (group_id, sub, group_role),
        )


def remove_group_member(group_id: int, sub: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM org_group_members WHERE group_id = %s AND sub = %s",
            (group_id, sub),
        )
        return (cur.rowcount or 0) > 0


def list_group_members(group_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, group_role, is_active, joined_at FROM org_group_members "
            "WHERE group_id = %s ORDER BY joined_at",
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_group_role(group_id: int, sub: str) -> Optional[str]:
    """Rôle EXPLICITE du sub dans le groupe ('group_admin'|'group_member') ou None.
    Pour le rôle effectif (escalade org_admin/platform), voir `roles.py`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT group_role FROM org_group_members WHERE group_id = %s AND sub = %s",
            (group_id, sub),
        ).fetchone()
        return row["group_role"] if row else None


def count_group_admins(group_id: int) -> int:
    return sum(1 for m in list_group_members(group_id) if m["group_role"] == "group_admin")


# --- groupe actif (mirroir org_store.get/set_active_org) --------------------

def get_active_group(sub: str) -> Optional[int]:
    """group_id du groupe actif du sub, ou None. L'index partiel
    `org_group_members_one_active` garantit au plus une ligne active par sub."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT group_id FROM org_group_members WHERE sub = %s AND is_active LIMIT 1",
            (sub,),
        ).fetchone()
        return int(row["group_id"]) if row else None


def list_groups_for_user(sub: str, org_id: Optional[int] = None) -> list[dict]:
    """Groupes auxquels le sub appartient (option : filtrés sur une org)."""
    q = (
        "SELECT g.id AS group_id, g.org_id, g.name, m.group_role, m.is_active, m.joined_at "
        "FROM org_group_members m JOIN org_groups g ON g.id = m.group_id WHERE m.sub = %s"
    )
    params: tuple = (sub,)
    if org_id is not None:
        q += " AND g.org_id = %s"
        params = (sub, org_id)
    q += " ORDER BY g.name"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def is_group_member(sub: str, group_id: int) -> bool:
    """`sub` est-il membre du groupe `group_id` ? (appartenance réelle, sans escalade
    — miroir du check de `set_active_group`, pour valider un override de session)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM org_group_members WHERE group_id = %s AND sub = %s",
            (group_id, sub),
        ).fetchone() is not None


def set_active_group(sub: str, group_id: int) -> bool:
    """Bascule le groupe actif du sub. Pose AUSSI l'org active sur l'org du groupe
    (invariant ADR 0012 : groupe actif ⊂ org active), atomiquement. False si le
    sub n'est pas membre du groupe (ou du groupe inconnu)."""
    g = get_group(group_id)
    if g is None:
        return False
    with _connect() as conn:
        with conn.transaction():
            in_group = conn.execute(
                "SELECT 1 FROM org_group_members WHERE group_id = %s AND sub = %s",
                (group_id, sub),
            ).fetchone()
            if not in_group:
                return False
            in_org = conn.execute(
                "SELECT 1 FROM org_members WHERE org_id = %s AND sub = %s",
                (g["org_id"], sub),
            ).fetchone()
            if not in_org:
                return False  # incohérence : membre groupe mais pas org
            # org active = org du groupe ; groupe actif = ce groupe (les deux UPDATE
            # respectent les index partiels one_active : exactement une TRUE).
            conn.execute(
                "UPDATE org_members SET is_active = (org_id = %s) WHERE sub = %s",
                (g["org_id"], sub),
            )
            conn.execute(
                "UPDATE org_group_members SET is_active = (group_id = %s) WHERE sub = %s",
                (group_id, sub),
            )
            return True


def clear_active_group(sub: str) -> None:
    """Désélectionne le groupe actif (revenir au niveau org). No-op si aucun."""
    with _connect() as conn:
        conn.execute(
            "UPDATE org_group_members SET is_active = FALSE WHERE sub = %s AND is_active",
            (sub,),
        )


# --- secrets de groupe (coffre chiffré, entity_type='group') ----------------

def get_group_secret(group_id: int, provider: str) -> Optional[str]:
    return credentials_store.get_credential("group", str(group_id), provider)


def has_group_secret(group_id: int, provider: str) -> bool:
    return credentials_store.has_credential("group", str(group_id), provider)


def set_group_secret(group_id: int, provider: str, api_key: str,
                     set_by: Optional[str] = None, meta: Optional[dict] = None) -> None:
    """Pose/rote un secret partagé du groupe. Mêmes providers org-partageables que
    les secrets d'org (validés par le registre)."""
    connectors.require_credential("org", provider)  # même éligibilité que l'org
    if not api_key:
        raise ValueError("api_key requise")
    credentials_store.set_credential(
        "group", str(group_id), provider, api_key, set_by=set_by, meta=meta)


def delete_group_secret(group_id: int, provider: str) -> bool:
    return credentials_store.clear_credential("group", str(group_id), provider)


def list_group_secrets(group_id: int) -> list[dict]:
    out: list[dict] = []
    for c in credentials_store.list_credentials("group", str(group_id)):
        entry = {"provider": c["connector"], "set_by": c["set_by"], "set_at": c["set_at"]}
        base_url = (c.get("meta") or {}).get("base_url")
        if base_url:
            entry["base_url"] = base_url
        out.append(entry)
    return out


# --- doctrine & skills du groupe (miroir org_store, table org_group_*) ------
#
# Même modèle versionné que la doctrine d'org : slug réservé BASE_SLUG = base,
# autres = skills. En clair (prose). normalize_slug/_snippet réutilisés d'org_store.

def _base_readme(group_id: int, version: Optional[int]) -> Optional[dict]:
    """Le readme d'équipe (`claude_md`) présenté comme une instruction, mais LU dans
    `guides` (delivery='init', ADR 0042 : le readme n'est plus une procédure — pas de
    version/slots/historique). None si absent ou version demandée."""
    if version is not None:
        return None
    from . import guide_store
    st = guide_store.get_init_guide("group", group_id)
    if st["updated_at"] is None:
        return None
    return {"group_id": group_id, "slug": BASE_SLUG, "title": "", "description": "",
            "body_md": st["body_md"], "version": 1, "set_by": None,
            "created_at": st["updated_at"], "updated_at": st["updated_at"]}


def get_group_instruction(group_id: int, slug: str,
                          version: Optional[int] = None) -> Optional[dict]:
    slug = normalize_slug(slug)
    if slug == BASE_SLUG:
        return _base_readme(group_id, version)
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT group_id, slug, title, description, body_md, version, set_by, "
                "created_at, updated_at FROM org_group_instructions "
                "WHERE group_id = %s AND slug = %s",
                (group_id, slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT group_id, slug, title, description, body_md, version, set_by, "
                "created_at FROM org_group_instruction_revisions "
                "WHERE group_id = %s AND slug = %s AND version = %s",
                (group_id, slug, version),
            ).fetchone()
        return dict(row) if row else None


def list_group_instructions(group_id: int, include_base: bool = False) -> list[dict]:
    where = "group_id = %s" if include_base else "group_id = %s AND slug <> %s"
    params: tuple = (group_id,) if include_base else (group_id, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT slug, title, description, version, updated_at "
            f"FROM org_group_instructions WHERE {where} ORDER BY slug",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def search_group_instructions(group_id: int, query: str,
                              include_base: bool = False) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    base_filter = "" if include_base else "AND slug <> %s "
    head: tuple = (group_id,) if include_base else (group_id, BASE_SLUG)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT slug, title, description, body_md, version, updated_at "
            "FROM org_group_instructions WHERE group_id = %s " + base_filter +
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


def set_group_instruction(group_id: int, slug: str, body_md: str,
                          title: Optional[str] = None, description: Optional[str] = None,
                          set_by: Optional[str] = None) -> int:
    """Crée/MAJ une instruction de groupe ; renvoie la nouvelle version + archive
    un snapshot. Sérialisé par (group, slug) via verrou advisory."""
    slug = normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if not (body_md or "").strip():
        raise ValueError("body_md requis")
    # Le readme d'équipe (claude_md) vit dans `guides` (ADR 0042) — prose plate.
    if slug == BASE_SLUG:
        from . import guide_store
        guide_store.set_init_guide("group", group_id, body_md)
        return 1
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         (f"gi:{group_id}:{slug}",))
            cur = conn.execute(
                "SELECT version, title, description FROM org_group_instructions "
                "WHERE group_id = %s AND slug = %s",
                (group_id, slug),
            ).fetchone()
            new_version = (cur["version"] + 1) if cur else 1
            new_title = title if title is not None else (cur["title"] if cur else "")
            new_desc = description if description is not None else (cur["description"] if cur else "")
            conn.execute(
                """
                INSERT INTO org_group_instructions
                    (group_id, slug, title, description, body_md, version, set_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (group_id, slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, version = EXCLUDED.version,
                    set_by = EXCLUDED.set_by, updated_at = NOW()
                """,
                (group_id, slug, new_title, new_desc, body_md, new_version, set_by),
            )
            conn.execute(
                """
                INSERT INTO org_group_instruction_revisions
                    (group_id, slug, version, title, description, body_md, set_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (group_id, slug, new_version, new_title, new_desc, body_md, set_by),
            )
            return new_version


def list_group_instruction_versions(group_id: int, slug: str) -> list[dict]:
    slug = normalize_slug(slug)
    if slug == BASE_SLUG:
        return []                                  # readme sans historique (ADR 0042)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT version, title, set_by, created_at FROM org_group_instruction_revisions "
            "WHERE group_id = %s AND slug = %s ORDER BY version DESC",
            (group_id, slug),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_group_instruction(group_id: int, slug: str) -> bool:
    slug = normalize_slug(slug)
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM org_group_instructions WHERE group_id = %s AND slug = %s",
                (group_id, slug),
            )
            removed = (cur.rowcount or 0) > 0
            conn.execute(
                "DELETE FROM org_group_instruction_revisions WHERE group_id = %s AND slug = %s",
                (group_id, slug),
            )
    return removed
