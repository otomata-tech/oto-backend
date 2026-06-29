"""Primitive de ressource possédée (ADR 0030) — le seam unique d'ownership.

Une ressource est identifiée par `(resource_type, resource_id)`. Sa **propriété**
vit sur la ressource (colonnes `owner_type`/`owner_id`), résolue ici via un registre
de *kinds* (`RESOURCE_KINDS`). Deux plans de permission, jamais confondus :

- **contenu** (`can_access`) = owner ∪ grants. *Privacy by default* : l'escalade de
  rôle ne donne **pas** le contenu d'une ressource perso (`owner_type='user'`).
- **gouvernance** (`can_govern`) = owner ∪ escalade `roles.py` (transférer / lister /
  révoquer / supprimer — **sans lire** le contenu).

La lecture opérateur d'une ressource perso reste l'exception **auditée** (view-as
REST, ADR 0023) — aucun chemin de lecture privilégié ici.

Sens unique (ADR 0004) : lit `db`/`roles`/`org_store`/`group_store`, jamais l'inverse.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import db, group_store, org_store, roles


# --- Scope de l'acteur (les principals sous lesquels il peut accéder) --------

@dataclass(frozen=True)
class AccessorScope:
    sub: str
    org_ids: list[int]
    group_ids: list[int]

    def principal_pairs(self) -> list[tuple[str, str]]:
        """(principal_type, principal_id) sous lesquels l'acteur reçoit des grants."""
        pairs: list[tuple[str, str]] = [("user", self.sub)]
        pairs += [("org", str(o)) for o in self.org_ids]
        pairs += [("group", str(g)) for g in self.group_ids]
        return pairs

    def owner_pairs(self) -> list[tuple[str, str]]:
        """(owner_type, owner_id) que l'acteur possède (perso + ses orgs/groupes)."""
        return self.principal_pairs()


def accessor_scope(sub: str) -> AccessorScope:
    org_ids = [int(o["org_id"]) for o in org_store.list_orgs_for_user(sub)]
    group_ids = [int(g["group_id"]) for g in group_store.list_groups_for_user(sub)]
    return AccessorScope(sub=sub, org_ids=org_ids, group_ids=group_ids)


# --- Registre des types de ressource ----------------------------------------

@dataclass(frozen=True)
class ResourceKind:
    # rid (str) -> (owner_type, owner_id) | None
    owner_getter: Callable[[str], Optional[tuple[str, str]]]
    # rid, new_owner_type, new_owner_id -> None (lève ValueError sur collision)
    reparent: Callable[[str, str, str], None]


RESOURCE_KINDS: dict[str, ResourceKind] = {}


def register_kind(resource_type: str, kind: ResourceKind) -> None:
    RESOURCE_KINDS[resource_type] = kind


def _kind(resource_type: str) -> ResourceKind:
    k = RESOURCE_KINDS.get(resource_type)
    if k is None:
        raise ValueError(f"unknown resource type `{resource_type}`")
    return k


def owner_of(resource_type: str, resource_id: str) -> Optional[tuple[str, str]]:
    return _kind(resource_type).owner_getter(resource_id)


# --- Plan CONTENU : can_access (owner ∪ grants) ------------------------------

def _owner_match_content(sub: str, owner_type: str, owner_id: str) -> bool:
    """L'acteur accède-t-il au contenu *en tant que* propriétaire (ou membre de
    l'org/groupe propriétaire) ? Pas d'escalade plateforme ici (privacy by default)."""
    if owner_type == "user":
        return sub == owner_id
    if owner_type == "org":
        return roles.is_org_member(sub, int(owner_id))
    if owner_type == "group":
        return roles.can_read_group(sub, int(owner_id))
    return False


def can_access(sub: str, resource_type: str, resource_id: str, want: str = "read") -> bool:
    """Plan CONTENU. `want` ∈ {read, write}. Owner-match (perso/org/groupe) donne
    read+write ; sinon un grant suffisant (write requis pour écrire)."""
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        return False
    if _owner_match_content(sub, owner[0], owner[1]):
        return True
    best = _best_grant(sub, resource_type, resource_id)
    if best is None:
        return False
    return want == "read" or best == "write"


def _best_grant(sub: str, resource_type: str, resource_id: str) -> Optional[str]:
    """Meilleure permission accordée à l'acteur (write > read), ou None."""
    scope = accessor_scope(sub)
    best: Optional[str] = None
    for ptype, pid in scope.principal_pairs():
        g = db.get_resource_grant(resource_type, resource_id, ptype, pid)
        if g is None:
            continue
        if g["permission"] == "write":
            return "write"
        best = "read"
    return best


# --- Plan GOUVERNANCE : can_govern (owner ∪ escalade roles.py) ----------------

def can_govern(sub: str, resource_type: str, resource_id: str) -> bool:
    """Plan GOUVERNANCE : transférer / partager / révoquer / supprimer, SANS lire le
    contenu. Owner ∪ escalade `roles.py` (platform_admin / org_admin / group_admin)."""
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        return False
    owner_type, owner_id = owner
    if owner_type == "user":
        return sub == owner_id or roles.is_platform_admin(sub)
    if owner_type == "org":
        return roles.is_org_admin(sub, int(owner_id))
    if owner_type == "group":
        return roles.can_admin_group(sub, int(owner_id))
    return False


# --- Mutations ----------------------------------------------------------------

def grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
    permission: str = "write", *, granted_by: Optional[str] = None,
) -> None:
    db.grant_resource(resource_type, resource_id, principal_type, principal_id,
                      permission, granted_by)


def revoke(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
) -> bool:
    return db.revoke_resource_grant(resource_type, resource_id, principal_type, principal_id)


def list_grants(resource_type: str, resource_id: str) -> list[dict]:
    return db.list_resource_grants(resource_type, resource_id)


def transfer(
    resource_type: str, resource_id: str, new_owner_type: str, new_owner_id: str,
) -> None:
    """Re-parente la ressource. Préserve l'UX non-destructive : l'ancien propriétaire
    **user** garde un accès `write` (passe en partagé) ; le nouveau propriétaire
    perd son éventuel grant (il est désormais owner)."""
    prev = owner_of(resource_type, resource_id)
    _kind(resource_type).reparent(resource_id, new_owner_type, new_owner_id)
    # Le nouveau propriétaire ne reste pas bénéficiaire de sa propre ressource.
    db.revoke_resource_grant(resource_type, resource_id, new_owner_type, new_owner_id)
    # L'ancien propriétaire user conserve un accès write (« tu passes en partagé »).
    if prev is not None and prev[0] == "user" and prev != (new_owner_type, new_owner_id):
        db.grant_resource(resource_type, resource_id, "user", prev[1], "write")


# --- Enregistrement du kind `datastore_namespace` (pilote ADR 0030) ----------

def _datastore_owner(rid: str) -> Optional[tuple[str, str]]:
    row = db.get_datastore_namespace_by_id(int(rid))
    if row is None or row.get("owner_id") is None:
        return None
    return (row["owner_type"], row["owner_id"])


def _datastore_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    db.reparent_datastore_namespace(int(rid), new_owner_type, new_owner_id)


register_kind(
    "datastore_namespace",
    ResourceKind(owner_getter=_datastore_owner, reparent=_datastore_reparent),
)
