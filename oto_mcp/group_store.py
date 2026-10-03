"""Accès DB du sous-palier GROUPE (départements / équipes d'une org, ADR 0012).

Miroir d'`org_store` au grain groupe. Les tables (`org_groups`,
`org_group_members`) sont déclarées dans `db._SCHEMA` ; leurs requêtes vivent ici.
Les secrets de groupe vivent dans le coffre chiffré `connector_credentials`
(entity_type='group'), comme ceux d'org.

Un groupe gouverne DEUX ressources par délégation de l'org (décision produit) :
- **procédures** — même table et même jeu de fonctions que l'org, keyé sur
  `(owner_type, owner_id)` : `org_store.<fn>('group', group_id, …)`. ⚠️ Il a existé
  ici, jusqu'au 31/08/2026, un SECOND jeu (`*_group_instruction*`) qui filtrait en
  dur `owner_type='group'` sur la même table — deux implémentations concurrentes qui
  avaient déjà divergé (slots, archivage). Elles ont fusionné, cf. oto-backend#681 ;
- **secrets partagés** (coffre, entity_type='group') — résolus avant ceux de l'org.

Sens unique (ADR 0004) : dépend de `db`/`org_store`/`credentials_store`/
`connectors`, jamais l'inverse. `org_store` n'importe PAS ce module (il manipule
`org_group_members` en SQL direct pour l'invariant org↔groupe, et lit l'org parente
d'une équipe de la même façon → pas de cycle).
"""
from __future__ import annotations

from typing import Optional

from . import providers, credentials_store
from .db import _connect

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


def org_ids_of_groups(group_ids: list[int]) -> dict[int, int]:
    """Org parente de chaque équipe d'un lot — une requête, pas une par équipe."""
    if not group_ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id FROM org_groups WHERE id = ANY(%s)", (list(group_ids),),
        ).fetchall()
        return {int(r["id"]): int(r["org_id"]) for r in rows}


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
    """Supprime un groupe (cascade : membres, guide, revisions). Les secrets
    de groupe (coffre) sont purgés explicitement (hors FK).

    ⚠️ Par la primitive du coffre et non par un `DELETE` brut (L6 pièce 2) : un
    retrait qui contourne l'entonnoir laisse les instances de l'équipe VIVANTES —
    des objets qui désignent des clés disparues, et qu'un binding ou une arête
    peuvent nommer."""
    with _connect() as conn:
        with conn.transaction():
            credentials_store.clear_entity_credentials("group", str(group_id),
                                                       conn=conn)
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


def default_group_in_org(sub: str, org_id: int) -> Optional[int]:
    """Équipe par défaut du sub dans `org_id` : l'active si elle ⊂ org, sinon
    celle qu'il a rejointe en premier ; None s'il n'est dans aucune équipe de
    l'org. Un membre d'équipe n'est jamais « sans équipe » (lu par
    `access.current_group`, aucune écriture)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT m.group_id FROM org_group_members m "
            "JOIN org_groups g ON g.id = m.group_id "
            "WHERE m.sub = %s AND g.org_id = %s "
            "ORDER BY m.is_active DESC, m.joined_at, m.group_id LIMIT 1",
            (sub, org_id),
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

def get_group_secret(group_id: int, provider: str, account: str = "") -> Optional[str]:
    return credentials_store.get_credential("group", str(group_id), provider, account)


def has_group_secret(group_id: int, provider: str) -> bool:
    return credentials_store.has_credential("group", str(group_id), provider)


def set_group_secret(group_id: int, provider: str, api_key: str,
                     set_by: Optional[str] = None, meta: Optional[dict] = None,
                     account: str = "") -> None:
    """Pose/rote un secret partagé du groupe. Mêmes providers org-partageables que
    les secrets d'org (validés par le registre)."""
    providers.require_credential("org", provider)  # même éligibilité que l'org
    if not api_key:
        raise ValueError("api_key requise")
    credentials_store.set_credential(
        "group", str(group_id), provider, api_key, set_by=set_by, meta=meta, account=account)


def delete_group_secret(group_id: int, provider: str, account: str = "") -> bool:
    return credentials_store.clear_credential("group", str(group_id), provider, account=account)


def list_group_secrets(group_id: int) -> list[dict]:
    out: list[dict] = []
    for c in credentials_store.list_credentials("group", str(group_id)):
        entry = {"provider": c["connector"], "set_by": c["set_by"], "set_at": c["set_at"]}
        base_url = (c.get("meta") or {}).get("base_url")
        if base_url:
            entry["base_url"] = base_url
        out.append(entry)
    return out
