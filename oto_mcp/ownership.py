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


def active_owner(org_id: Optional[int]) -> Optional[tuple[str, str]]:
    """Owner-pair du CONTEXTE COURANT (= l'org active) — le pendant `ownership` de
    `access.current_org` (ADR 0023).

    **Règle de scoping** : toute LISTE DE CONTENU possédé (datastore, projets…) scope
    là-dessus → charger une org ne montre QUE ses ressources. `accessor_scope`/
    `owner_pairs` (union de TOUTES les orgs de l'acteur) est réservé au plan
    GOUVERNANCE / découverte cross-org (ex. `oto_resource list`, bibliothèque de
    modèles). Mélanger les deux = fuite cross-org *fail-open* (le superset expose
    plus que le contexte) — cf. garde-fou `tests/test_owner_scope_tripwire.py`.

    Retourne `None` si aucune org active (le caller tranche : 400 en capacité, liste
    vide en rendu). Post-abolition du perso, `current_org` est toujours posé."""
    return None if org_id is None else ("org", str(org_id))


def active_org_principals(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """Principals du CONTEXTE de l'org active sous lesquels une ressource est visible
    ici : l'org active, l'acteur, et ses groupes DANS cette org. Source unique du
    scoping par-contexte (ADR 0023), partagée par les listes et `visible_in_org`."""
    owner = active_owner(org_id)
    if owner is None:
        return []
    return [owner, ("user", sub)] + [
        ("group", str(g["group_id"]))
        for g in group_store.list_groups_for_user(sub, org_id)]


def visible_in_org(sub: str, org_id: Optional[int],
                   resource_type: str, resource_id: str) -> bool:
    """Une ressource possédée est-elle visible DANS le contexte de l'org `org_id` ?
    Possédée par cette org OU partagée à un principal du contexte — le pendant PAR-ID
    du scoping de liste (`active_owner`). `can_access` (union de TOUTES les orgs de
    l'acteur) est trop large pour une lecture/ouverture contextuelle : il laisse
    atteindre une ressource d'une AUTRE de mes orgs, hors contexte (fuite cross-org,
    cf. l'incident projet). À utiliser pour toute lecture/action par-id scopée à l'org
    active ; `can_access` reste le plan CONTENU (découverte/partage cross-org)."""
    owner = active_owner(org_id)
    if owner is None:
        return False
    o = owner_of(resource_type, resource_id)
    if o is not None and (str(o[0]), str(o[1])) == owner:
        return True
    return any(db.get_resource_grant(resource_type, resource_id, pt, pid) is not None
               for pt, pid in active_org_principals(sub, org_id))


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


def org_can_access(org_id: int, resource_type: str, resource_id: str,
                   want: str = "read") -> bool:
    """Plan CONTENU vu depuis un PRINCIPAL ORG (pas un user) — pendant `sub`-less de
    `can_access`, pour un endpoint MCP agissant SOUS L'AUTORITÉ d'une org (secret +
    opt-in datastore, ADR 0032). Accès si l'org POSSÈDE la ressource, ou si un grant
    `principal=('org', org_id)` suffisant existe (write requis pour écrire). Pas
    d'escalade de rôle : c'est du contenu, pas de la gouvernance."""
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        return False
    if (str(owner[0]), str(owner[1])) == ("org", str(org_id)):
        return True
    g = db.get_resource_grant(resource_type, resource_id, "org", str(org_id))
    if g is None:
        return False
    return want == "read" or g["permission"] == "write"


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


# --- Plan GOUVERNANCE : can_transfer (structure) / can_govern (grantable) ------

def can_transfer(sub: str, resource_type: str, resource_id: str) -> bool:
    """Gouvernance STRUCTURELLE : transfert de propriété — owner ∪ escalade `roles.py`
    (platform_admin / org_admin / group_admin), **jamais** un simple `gérant` (ADR 0048
    §3 : le gérant gouverne mais ne peut ni retirer l'owner ni se l'approprier). C'est
    l'ancienne sémantique de `can_govern`, conservée pour le seul transfert."""
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


def _has_manager_grant(sub: str, resource_type: str, resource_id: str) -> bool:
    """L'acteur détient-il un grant de rôle `manager` (gérant) sur la ressource, via
    l'un de ses principals (perso / org / groupe) ? — ADR 0048, gouvernance GRANTABLE."""
    for ptype, pid in accessor_scope(sub).principal_pairs():
        g = db.get_resource_grant(resource_type, resource_id, ptype, pid)
        if g is not None and g.get("role") == "manager":
            return True
    return False


def can_govern(sub: str, resource_type: str, resource_id: str) -> bool:
    """Plan GOUVERNANCE (ADR 0048) : re-partager / révoquer / supprimer / publier, SANS
    lire le contenu. **Grantable** = owner ∪ **grant `gérant`** ∪ escalade `roles.py`. Le
    transfert de propriété, lui, exclut le gérant → `can_transfer`."""
    if owner_of(resource_type, resource_id) is None:
        return False
    return can_transfer(sub, resource_type, resource_id) \
        or _has_manager_grant(sub, resource_type, resource_id)


# --- Mutations ----------------------------------------------------------------

def grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
    permission: Optional[str] = None, *, role: Optional[str] = None,
    granted_by: Optional[str] = None,
) -> None:
    """Accorde un RÔLE (ADR 0048) à un principal. `role` ∈ {viewer, editor, manager}
    prime ; à défaut `permission` read/write est mappé (rétro-compat)."""
    db.grant_resource(resource_type, resource_id, principal_type, principal_id,
                      permission=permission, granted_by=granted_by, role=role)


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


def _project_owner(rid: str) -> Optional[tuple[str, str]]:
    row = db.get_project_by_id(int(rid))
    if row is None or row.get("owner_id") is None:
        return None
    return (row["owner_type"], row["owner_id"])


def _project_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    db.reparent_project(int(rid), new_owner_type, new_owner_id)


register_kind(
    "project",
    ResourceKind(owner_getter=_project_owner, reparent=_project_reparent),
)


# --- Kind `doctrine` (épic « couverture des autres types », prérequis #52) ----
# Une doctrine est TOUJOURS un objet d'org : son owner DÉRIVE d'`org_instructions.
# org_id` (pas de colonnes owner_* — « derive don't duplicate »). resource_id =
# l'id surrogate stable (ADR 0032 « stop using slug »). Le partage (grant read à
# une org cliente) rend la doctrine lisible cross-org par id via oto_get_doctrine.

def _doctrine_owner(rid: str) -> Optional[tuple[str, str]]:
    if not str(rid).isdigit():   # relique : des liens legacy portent encore un slug
        return None
    row = org_store.get_instruction_by_id(int(rid))
    if row is None:
        return None
    return ("org", str(row["org_id"]))


def _doctrine_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    if new_owner_type != "org":
        raise ValueError("une doctrine est un objet d'org — transfert vers une org uniquement")
    org_store.reparent_instruction(int(rid), int(new_owner_id))


register_kind(
    "doctrine",
    ResourceKind(owner_getter=_doctrine_owner, reparent=_doctrine_reparent),
)
