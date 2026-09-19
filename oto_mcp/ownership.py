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


def project_scope_owners(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """Owners du CONTEXTE projet de l'org active (lot 3 Ship 1, factorisation du
    scoping d'`oto_project op=list`) : l'org active + ses pôles (ADR 0049 — mes
    équipes, ou TOUTES si org_admin, même règle que `can_read_group`). Parité
    gardée par tripwire (`test_search_scope_tripwire`)."""
    owner = active_owner(org_id)
    if owner is None:
        return []
    if roles.is_org_admin(sub, int(org_id)):        # type: ignore[arg-type]
        gids = [int(g["id"]) for g in group_store.list_groups(int(org_id))]  # type: ignore[arg-type]
    else:
        gids = [int(g["group_id"]) for g in group_store.list_groups_for_user(sub, org_id)]
    return [owner] + [("group", str(g)) for g in gids]


def accessible_project_ids(sub: str, org_id: Optional[int],
                           want: str = "read") -> list[int]:
    """Ids des projets accessibles DANS l'org active — le scoping ENSEMBLISTE
    d'`op=list` factorisé (lot 3) : owned par le contexte (org + pôles) ∪ partagés
    aux principals du contexte. Sert la recherche et l'îlot « Dernières
    modifications » (`want='read'`) ; `want='write'` n'ajoute que les grants write. **Jamais `can_access`**
    (cross-org par construction) — cf. invariants du plan lot 3."""
    return accessible_project_ids_by_provenance(sub, org_id, want=want)["all"]


def accessible_project_ids_by_provenance(sub: str, org_id: Optional[int],
                                         want: str = "read") -> dict[str, list[int]]:
    """Même ensemble qu'`accessible_project_ids`, mais qui dit la PROVENANCE de
    chaque id (oto-backend, feedback #1005/#1006 : un résultat de recherche
    partagé cross-org peut être pris à tort pour une ressource propre de l'org
    auditée). `own` = possédé par le contexte de l'org active (org + pôles + mes
    projets perso de cette org) ; `granted` = visible UNIQUEMENT via un partage
    cross-org (`db.list_projects_granted_to`) — jamais les deux à la fois, un id
    possédé par le contexte n'entre pas dans `granted` même s'il est AUSSI
    partagé explicitement. `all` = l'union, dans le même ordre qu'avant (owned
    puis membre puis grants) — c'est elle que sert `accessible_project_ids`."""
    owners = project_scope_owners(sub, org_id)
    if not owners:
        return {"all": [], "own": [], "granted": []}
    own = [int(r["id"]) for r in db.list_projects_for_owners(owners)]
    seen = set(own)
    # Scope MEMBRE (ADR 0030 amendé) : mes projets perso de CETTE org (`context_org`),
    # possédés → read+write. En PARITÉ STRICTE avec `oto_project op=list` (même seam
    # `db.list_member_projects`) — sinon « cherchable ⇔ lisible » ment (tripwire
    # `test_search_scope_tripwire`). org_id non-None ici (owners non vide).
    for r in db.list_member_projects(sub, int(org_id)):  # type: ignore[arg-type]
        rid = int(r["id"])
        if rid not in seen:
            own.append(rid)
            seen.add(rid)
    granted: list[int] = []
    for r in db.list_projects_granted_to(active_org_principals(sub, org_id)):
        rid = int(r["id"])
        if rid in seen:
            continue
        if want == "write" and r.get("permission") != "write":
            continue
        granted.append(rid)
        seen.add(rid)
    return {"all": own + granted, "own": own, "granted": granted}


def visible_in_org(sub: str, org_id: Optional[int],
                   resource_type: str, resource_id: str) -> bool:
    """Une ressource possédée est-elle visible DANS le contexte de l'org `org_id` ?
    Possédée par cette org OU partagée à un principal du contexte — le pendant PAR-ID
    du scoping de liste (`active_owner`). `can_access` (union de TOUTES les orgs de
    l'acteur) est trop large pour une lecture/ouverture contextuelle : il laisse
    atteindre une ressource d'une AUTRE de mes orgs, hors contexte (fuite cross-org,
    cf. l'incident projet). À utiliser pour toute lecture/action par-id scopée à l'org
    active ; `can_access` reste le plan CONTENU (découverte/partage cross-org)."""
    o = owner_of(resource_type, resource_id)
    if o is not None and owner_in_scope(sub, org_id, o):
        return True
    return any(db.get_resource_grant(resource_type, resource_id, pt, pid) is not None
               for pt, pid in active_org_principals(sub, org_id))


def owner_in_scope(sub: str, org_id: Optional[int],
                   owner: Optional[tuple]) -> bool:
    """Ce PROPRIÉTAIRE est-il à portée de cette personne, dans ce contexte d'org ?

    La règle de portée, **une seule fois pour toute la plateforme**. Elle ne connaît
    ni les grants ni le type de ressource : c'est ce qui la rend partageable entre des
    mondes dont les partages ne se rangent pas au même endroit (le datastore les lit
    dans `resource_grants` par `(type, id)` ; les nœuds les traduisent depuis leurs
    types d'origine, `db/shell.resolve_grant_nodes`). Chaque monde garde SA résolution
    de grants et appelle celle-ci pour la portée.

    Elle existe parce que la règle était écrite deux fois et **avait déjà divergé**
    (#682) : le monde des nœuds ne traitait ni le cran plateforme ni l'escalade
    d'équipe, si bien qu'un même nœud était invisible par une porte et lisible par
    l'autre. Recopier les branches manquantes aurait rouvert l'écart au premier
    changement — c'est exactement ce que le commentaire de `_lisible` prédisait.

    Quatre voies, aucune n'est une fuite cross-org :
    """
    if owner is None:
        return False
    otype, oid = str(owner[0]), str(owner[1])
    # 1. L'org active elle-même.
    if (otype, oid) == active_owner(org_id):
        return True
    # 2. Scope MEMBRE (ADR 0030 amendé) : ma ressource perso m'est visible dans tout
    #    contexte où je suis — c'est la MIENNE. Le plan de LISTE, lui, la range dans
    #    son org de contexte (`list_member_projects`).
    if otype == "user" and oid == sub:
        return True
    # 3. ADR 0049 : une ressource d'ÉQUIPE appartient au contexte de son org PARENTE —
    #    à portée ssi l'équipe est dans cette org ET que l'acteur peut la lire (membre,
    #    ou escalade org_admin/platform via `roles.can_read_group`).
    if otype == "group":
        g = group_store.get_group(int(oid))
        return bool(g is not None and org_id is not None
                    and int(g["org_id"]) == int(org_id)
                    and roles.can_read_group(sub, int(oid)))
    # 4. ADR 0049 : le cran PLATEFORME (bibliothèque) est lisible dans tout contexte.
    return otype == "platform"


# --- Registre des types de ressource ----------------------------------------

@dataclass(frozen=True)
class ResourceKind:
    owner_getter: Callable[[str], Optional[tuple[str, str]]]  # rid -> (owner_type, owner_id) | None
    reparent: Callable[[str, str, str], None]  # rid, new_type, new_id ; ValueError si refus
    # rid -> (type, id) du parent qui la GOUVERNE seul (une page → son projet) : cf. `can_govern`.
    governed_by: Optional[Callable[[str], Optional[tuple[str, str]]]] = None


#: Le type de ressource d'un tableau du datastore, **tel qu'il est ÉCRIT EN BASE**
#: (`resource_grants.resource_type`). ⚠️ Ce n'est pas un mot de vocabulaire : c'est
#: une valeur persistée, que 15 partages de production portent aujourd'hui. La
#: renommer sans migrer les lignes ferait disparaître ces partages EN SILENCE —
#: aucune erreur, juste des droits qui s'évaporent. Elle a survécu au renommage de
#: `namespace` en `datastore` pour cette raison, et elle est nommée ici pour que le
#: prochain renommage la trouve au lieu de la traverser.
TYPE_RESSOURCE_DATASTORE = "datastore_namespace"

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
    if owner_type == "platform":
        # ADR 0049 : cran bibliothèque — l'ÉCRITURE owner-match est réservée à l'admin
        # plateforme ; la lecture universelle vit dans `can_access` (want-aware).
        return roles.is_platform_admin(sub)
    return False


def can_access(sub: str, resource_type: str, resource_id: str, want: str = "read") -> bool:
    """Plan CONTENU. `want` ∈ {read, write}. Owner-match (perso/org/groupe) donne
    read+write ; sinon un grant suffisant (write requis pour écrire)."""
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        return False
    # ADR 0049 : une ressource PLATFORM-owned (bibliothèque) est lisible par tout
    # utilisateur authentifié — un modèle est fait pour être lu et copié. L'écriture
    # reste l'owner-match (admin plateforme) ou un grant write.
    if owner[0] == "platform" and want == "read":
        return True
    if _owner_match_content(sub, owner[0], owner[1]):
        return True
    best = _best_grant(sub, resource_type, resource_id)
    if best is None:
        return False
    return want == "read" or best == "write"


def owns(sub: str, resource_type: str, resource_id: str) -> bool:
    """L'acteur est-il PROPRIÉTAIRE du contenu — lui, son org, son équipe — par
    opposition à un tiers qui n'y accède que par un GRANT ?

    Même prédicat que la branche owner de `can_access`, isolé parce qu'un cran a
    désormais besoin de distinguer *à qui la donnée appartient* de *qui a le droit
    d'y écrire* : le forçage d'une colonne verrouillée (#658). Sur un tableau partagé
    en écriture, le partenaire ÉCRIT sans POSSÉDER — confondre les deux rendrait le
    verrou inopérant, il ne protégerait plus de personne.

    ⚠️ Ce n'est PAS `can_govern` et ça ne le remplace pas : les deux ensembles se
    croisent sans s'inclure (un membre d'org possède sans gouverner ; un gérant
    gouverne sans posséder). Un cran qui veut les deux les demande tous les deux."""
    owner = owner_of(resource_type, resource_id)
    return owner is not None and _owner_match_content(sub, owner[0], owner[1])


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

def _can_transfer_owner(sub: str, owner_type: str, owner_id: str) -> bool:
    """L'acteur peut-il transférer une ressource dont l'owner serait `(owner_type,
    owner_id)` ? — owner ∪ escalade `roles.py`, **jamais** un simple gérant. Prend
    l'owner en PARAMÈTRE (pas résolu en DB) pour servir aussi la PRÉDICTION du garde-fou
    anti-lockout (`would_retain_control`), sans muter la ressource."""
    if owner_type == "user":
        return sub == owner_id or roles.is_platform_admin(sub)
    if owner_type == "org":
        return roles.is_org_admin(sub, int(owner_id))
    if owner_type == "group":
        return roles.can_admin_group(sub, int(owner_id))
    if owner_type == "platform":
        return roles.is_platform_admin(sub)   # ADR 0049 : bibliothèque = gouvernance plateforme
    return False


def can_transfer(sub: str, resource_type: str, resource_id: str) -> bool:
    """Gouvernance STRUCTURELLE : transfert de propriété — owner ∪ escalade `roles.py`
    (platform_admin / org_admin / group_admin), **jamais** un simple `gérant` (ADR 0048
    §3 : le gérant gouverne mais ne peut ni retirer l'owner ni se l'approprier). C'est
    l'ancienne sémantique de `can_govern`, conservée pour le seul transfert."""
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        return False
    return _can_transfer_owner(sub, owner[0], owner[1])


def would_retain_control(sub: str, new_owner_type: str, new_owner_id: str) -> bool:
    """Garde-fou anti-lockout : après un transfert vers `(new_owner_type, new_owner_id)`,
    l'acteur pourrait-il encore RÉCUPÉRER la ressource (la re-transférer) ? Faux ⇒ il perd
    tout contrôle (cession à un tiers, ou à une org/équipe qu'il n'administre pas) → l'UI/
    le MCP exige alors une confirmation explicite. Le platform_admin retient toujours (il
    rattrape n'importe quelle ressource)."""
    return _can_transfer_owner(sub, new_owner_type, new_owner_id)


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
    if (k := RESOURCE_KINDS.get(resource_type)) and k.governed_by:  # une page → son projet
        return (cible := k.governed_by(resource_id)) is not None and can_govern(sub, *cible)
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


def revoke(resource_type: str, resource_id: str, principal_type: str, principal_id: str) -> bool:
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
    row = db.get_datastore_by_id(int(rid))
    if row is None or row.get("owner_id") is None:
        return None
    return (row["owner_type"], row["owner_id"])


def _datastore_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    db.reparent_datastore(int(rid), new_owner_type, new_owner_id)


register_kind(
    TYPE_RESSOURCE_DATASTORE,
    ResourceKind(owner_getter=_datastore_owner, reparent=_datastore_reparent),
)


def _project_owner(rid: str) -> Optional[tuple[str, str]]:
    row = db.get_project_by_id(int(rid))
    if row is None or row.get("owner_id") is None:
        return None
    return (row["owner_type"], row["owner_id"])


def _project_context_org(row: dict, new_owner_id: str) -> Optional[int]:
    """Où RANGER un projet qui devient perso (`owner_type='user'`) — son `context_org_id`.

    Un projet perso n'est listé que dans son org de contexte (`db.list_member_projects`),
    donc sans ce calcul un transfert vers une personne produit un projet **invisible
    partout**, y compris pour son nouveau propriétaire. On garde le projet là où il
    travaillait déjà (org détentrice, org du groupe détenteur, ou contexte courant) si le
    destinataire y est membre ; sinon on retombe sur SON org perso — jamais NULL."""
    prev_type, prev_id = row.get("owner_type"), row.get("owner_id")
    candidate: Optional[int] = None
    if prev_type == "org" and prev_id:
        candidate = int(prev_id)
    elif prev_type == "group" and prev_id:
        g = group_store.get_group(int(prev_id))
        candidate = int(g["org_id"]) if g and g.get("org_id") else None
    elif prev_type == "user" and row.get("context_org_id"):
        candidate = int(row["context_org_id"])
    if candidate is not None and roles.is_org_member(new_owner_id, candidate):
        return candidate
    return org_store.ensure_personal_org(new_owner_id)


def _project_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    ctx: Optional[int] = None
    if new_owner_type == "user":
        row = db.get_project_by_id(int(rid))
        ctx = _project_context_org(row or {}, new_owner_id)
    db.reparent_project(int(rid), new_owner_type, new_owner_id, context_org_id=ctx)


register_kind(
    "project",
    ResourceKind(owner_getter=_project_owner, reparent=_project_reparent),
)


# --- Kind `guide` (épic « couverture des autres types », prérequis #52) ----
# L'owner d'une procédure est porté par `org_instructions.owner_type/owner_id` —
# 'org' ou 'group' (#681 ; 'user' = phase 2). resource_id = l'id surrogate stable
# (ADR 0032 « stop using slug »). Le partage (grant read à une org cliente) rend la
# procédure lisible cross-org par id via oto_procedure(op='get').

def _guide_owner(rid: str) -> Optional[tuple[str, str]]:
    if not str(rid).isdigit():   # relique : des liens legacy portent encore un slug
        return None
    row = org_store.get_instruction_by_id(int(rid))
    if row is None:
        return None
    # `owner_type`/`owner_id` sont NOT NULL depuis le backfill de boot : plus de filet
    # `("org", org_id)`. Ce filet-là RELISAIT le scope sur la ligne au lieu de le tenir
    # de la colonne prévue, et fabriquait un propriétaire (« org #None ») qui traversait
    # le seam d'autorisation sans lever. Une ligne sans propriétaire est une incohérence
    # de données : elle se signale, elle ne se devine pas (#681).
    if not row.get("owner_type") or not row.get("owner_id"):
        raise ValueError(f"procédure #{rid} sans propriétaire (owner_type/owner_id vides)")
    return (str(row["owner_type"]), str(row["owner_id"]))


def _guide_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    """Déplace une procédure entre paliers — org ↔ équipe (#681).

    Le palier PERSONNEL (`user`) reste fermé : `org_instructions.org_id` est NOT NULL
    et une personne n'a pas d'org parente ; l'ouvrir est la phase 2 du lot. Le refus
    dit lequel des deux manque plutôt que « un guide est un objet d'org », qui était
    faux depuis la fusion des procédures d'équipe."""
    org_store.move_instruction(int(rid), new_owner_type, new_owner_id)


register_kind(
    "doctrine",
    ResourceKind(owner_getter=_guide_owner, reparent=_guide_reparent),
)
