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

**Vue bornée (oto#270)** : quand un org_admin « voit en tant que » un membre de son org
O, `session_org.current_view_as_bound_org()` vaut O et ce seam ne rend visible que ce
que le membre voit DANS O (`vue_bornee`, `visible_in_org`). Hors de cette vue, chaque
fonction suit son chemin d'avant, à l'identique.

Sens unique (ADR 0004) : lit `db`/`roles`/`org_store`/`group_store`, jamais l'inverse.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import db, group_store, org_origin, org_store, roles, session_org


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


def vue_bornee() -> Optional[int]:
    """L'org O d'une vue « en tant que » posée par un org_admin (oto#270), sinon None.

    Posée par `ViewAsMiddleware` après ses gardes, jamais ailleurs. Toute fonction de
    ce seam qui la lit garde, quand elle vaut None, son chemin d'avant À L'IDENTIQUE."""
    return session_org.current_view_as_bound_org()


def accessor_scope(sub: str) -> AccessorScope:
    borne = vue_bornee()
    if borne is None:
        org_ids = [int(o["org_id"]) for o in org_store.list_orgs_for_user(sub)]
        group_ids = [int(g["group_id"]) for g in group_store.list_groups_for_user(sub)]
    else:
        # Vue bornée : l'acteur n'existe que dans O — ni ses autres orgs, ni leurs
        # équipes. C'est ce qui ferme la découverte cross-org (`can_access`, partages).
        org_ids = [borne] if roles.is_org_member(sub, borne) else []
        group_ids = [int(g["group_id"])
                     for g in group_store.list_groups_for_user(sub, borne)]
    # Jeton de délégation : les partages reçus ne comptent que dans l'org du travail
    # (`verrou_org.py`) — ni les autres orgs du porteur, ni leurs équipes.
    from . import verrou_org
    v = verrou_org.courant()
    ecart = v is not None and any(o != v.org_id for o in org_ids)
    if (verrou := verrou_org.borne(sub, route="accessor_scope", ecart=ecart)) is not None:
        org_ids = [o for o in org_ids if o == verrou.org_id]
        group_ids = [g for g in group_ids
                     if (group_store.get_group(g) or {}).get("org_id") == verrou.org_id]
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
    ici : l'org active, l'acteur, et ses groupes DANS cette org. Le plan de l'ACCÈS
    par id (`visible_in_org`, ADR 0023) ; les LISTES en retirent l'acteur hors de son
    org perso (`principaux_de_liste`, décision du 28/09/2026)."""
    owner = active_owner(org_id)
    if owner is None:
        return []
    return [owner, ("user", sub)] + [
        ("group", str(g["group_id"]))
        for g in group_store.list_groups_for_user(sub, org_id)]


def org_perso_de(sub: str, org_id: Optional[int]) -> bool:
    """`org_id` porte-t-elle l'étiquette d'org PERSO de `sub` (l'org créée à son
    inscription) ? Depuis le 29/09/2026, une org perso est fonctionnellement une org
    comme une autre ; l'étiquette ne sert plus qu'à désigner la MAISON de ce qui n'a pas
    d'org de création (`perso_de_la_liste`, le `tout` de `mes_objets_ici`) et à
    interdire à son propriétaire de la quitter. Même source que
    `me.active_org_is_personal` (`org_store`)."""
    return org_id is not None and org_store.get_personal_org(sub) == int(org_id)


def perso_de_la_liste(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """`[("user", sub)]` dans l'org perso de `sub`, `[]` partout ailleurs.

    Le principal PERSONNEL d'une liste, dans la MAISON de ce qui n'a pas d'org de
    création — comme propriétaire (mes objets perso, dont ceux qui ne portent pas de
    `context_org_id` : procédures, guides, nœuds perso) et comme destinataire (ce qui
    est partagé à moi en personne, qui n'appartient à aucune org et que la lentille
    « moi » sert aussi dans toute org). Ailleurs, mes objets perso qui PORTENT leur org
    de création passent par `mes_objets_ici` ; aucun objet d'un autre membre ni aucun
    partage nominatif n'entre dans la liste.

    ⚠️ Un filtre de LISTE, jamais un droit : `active_org_principals` et
    `owner_in_scope` gardent le principal personnel, parce qu'ils servent l'accès par
    id (`visible_in_org`), qui ne change pas — un projet ou un tableau perso s'ouvre
    toujours par son identifiant depuis n'importe quelle org."""
    return [("user", sub)] if org_perso_de(sub, org_id) else []


def principaux_de_liste(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """Principals sous lesquels une LISTE rend ce qui est possédé ou partagé dans
    l'org `org_id` : l'org, mes équipes dans cette org, et moi SEULEMENT dans mon org
    perso (`perso_de_la_liste`). Source unique des listes de projets, de tableaux, de
    pages reçues (`scope="org"`) et de la recherche — « cherchable ⇔ lisible » tient
    parce qu'elles lisent toutes celle-ci."""
    moi = perso_de_la_liste(sub, org_id)
    return [p for p in active_org_principals(sub, org_id) if p[0] != "user"
            or p in moi]


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


def project_list_owners(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """Propriétaires COLLECTIFS des projets qu'une LISTE rend dans l'org `org_id` : ceux
    du contexte (`project_scope_owners` — l'org et ses pôles). Mes projets PERSONNELS
    n'y sont pas : ils passent par `mes_objets_ici`, qui les range dans l'org où je
    les ai créés. Source unique d'`op=list`, `op=list_templates`, `archived=true`, du
    rail et de la recherche (`projets_possedes_ici`)."""
    return project_scope_owners(sub, org_id)


def mes_objets_ici(sub: Optional[str], org_id: Optional[int]
                   ) -> Optional[tuple[str, int, bool]]:
    """`(sub, org, tout)` : les objets PERSONNELS (`owner_type='user'`, projets et
    tableaux — ceux qui portent leur org de création, `context_org_id`) que `sub` voit
    dans la liste de l'org `org_id` : ceux qu'il y a CRÉÉS (`context_org_id = org`) et,
    dans son org perso (`tout`), tous ses objets personnels. `None` : aucun.

    Décisions d'Alexis du 29/09/2026 : un objet appartient à qui le crée et vit dans
    l'org où il l'a créé ; son propriétaire l'y voit TOUJOURS, que l'org porte
    l'étiquette perso ou non — « perso » n'est qu'une étiquette, fonctionnellement une
    org comme une autre. Les autres membres ne le voient que s'il le leur partage : la
    clause ne porte que sur `sub`. `tout` est la maison des objets sans org de
    création (legacy, ou créés hors de toute org) : l'org créée à l'inscription.
    En vue bornée (oto#270), seul le contexte O compte."""
    if not sub or org_id is None:
        return None
    tout = vue_bornee() is None and org_perso_de(sub, org_id)
    return (sub, int(org_id), tout)


def mes_tableaux_ici(createur: Optional[tuple[str, int, bool]]) -> list[dict]:
    """Mes tableaux PERSONNELS que la liste de l'org rend, selon `mes_objets_ici` : ceux
    créés dans cette org (`context_org_id`) et, dans mon org perso (`tout`), tous.
    Source unique de la liste des tableaux et de la recherche (parité « cherchable ⇔
    lisible »). `[]` sans `createur`."""
    if createur is None:
        return []
    csub, corg, tout = createur
    return [n for n in db.list_datastores_for_owners([("user", csub)])
            if tout or n.get("context_org_id") == corg]


def projets_possedes_ici(sub: str, org_id: Optional[int], *,
                         extra: Optional[list] = None, **kw) -> list[dict]:
    """Les projets POSSÉDÉS que la liste de l'org `org_id` rend : ceux de l'org et de
    ses pôles (`project_list_owners`), plus mes projets personnels créés ici
    (`mes_objets_ici`). `extra` ajoute des propriétaires (la plateforme, pour les
    modèles) ; `kw` passe à `db.list_projects_for_owners`. `[]` sans org active."""
    owners = project_list_owners(sub, org_id)
    if not owners:
        return []
    return db.list_projects_for_owners(owners + list(extra or []),
                                       createur=mes_objets_ici(sub, org_id), **kw)


def accessible_project_ids(sub: str, org_id: Optional[int],
                           want: str = "read") -> list[int]:
    """Ids des projets accessibles DANS l'org active — le scoping ENSEMBLISTE
    d'`op=list` factorisé (lot 3) : possédés par la liste (`project_list_owners`) ∪
    partagés à ses principals (`principaux_de_liste`). Sert la recherche et l'îlot
    « Dernières modifications » (`want='read'`) ; `want='write'` n'ajoute que les
    grants write. **Jamais `can_access`** (cross-org par construction) — cf.
    invariants du plan lot 3. PARITÉ STRICTE avec `oto_project op=list` (mêmes deux
    seams) — sinon « cherchable ⇔ lisible » ment (tripwire
    `test_search_scope_tripwire`)."""
    ids = [int(r["id"]) for r in projets_possedes_ici(sub, org_id)]
    if not ids and not project_list_owners(sub, org_id):
        return []
    seen = set(ids)
    for r in db.list_projects_granted_to(principaux_de_liste(sub, org_id)):
        rid = int(r["id"])
        if rid in seen:
            continue
        if want == "write" and r.get("permission") != "write":
            continue
        ids.append(rid)
        seen.add(rid)
    # Vue bornée : « cherchable ⇔ lisible » tient aussi en vue — la liste passe par la
    # même règle que la lecture par id (un partage personnel reçu n'y entre pas).
    return borner_a_la_vue(sub, "project", ids)


def visible_in_org(sub: str, org_id: Optional[int],
                   resource_type: str, resource_id: str) -> bool:
    """Une ressource possédée est-elle visible DANS le contexte de l'org `org_id` ?
    Possédée par cette org OU partagée à un principal du contexte — le pendant PAR-ID
    du scoping de liste (`active_owner`). `can_access` (union de TOUTES les orgs de
    l'acteur) est trop large pour une lecture/ouverture contextuelle : il laisse
    atteindre une ressource d'une AUTRE de mes orgs, hors contexte (fuite cross-org,
    cf. l'incident projet). À utiliser pour toute lecture/action par-id scopée à l'org
    active ; `can_access` reste le plan CONTENU (découverte/partage cross-org).

    **En vue bornée à O (oto#270)**, la règle se resserre, ici et une seule fois :
    - le contexte est O, et rien d'autre ;
    - une ressource PERSO du membre n'est visible que si elle DESCEND dans O : son
      kind la range dans une org (`context_org`, directement ou par son parent) et
      c'est O ; un kind qui ne range pas ses ressources perso (tableau, procédure
      personnelle) les fait suivre la personne partout, O compris ;
    - un partage PERSONNEL reçu (`principal = user`) ne compte pas : il n'appartient à
      aucune org, c'est l'espace du membre, pas celui de O."""
    borne = vue_bornee()
    if borne is not None and (org_id is None or int(org_id) != borne):
        return False
    o = owner_of(resource_type, resource_id)
    if o is not None and owner_in_scope(sub, org_id, o) and not (
            borne is not None
            and _perso_range_hors_de(sub, resource_type, resource_id, o, borne)):
        return True
    principals = active_org_principals(sub, org_id)
    if borne is not None:
        principals = [p for p in principals if p[0] != "user"]
    return any(db.get_resource_grant(resource_type, resource_id, pt, pid) is not None
               for pt, pid in principals)


def _rangement(resource_type: str, resource_id: str) -> tuple[bool, Optional[int]]:
    """`(le kind range-t-il ses ressources perso dans une org ?, l'org de rangement)`.

    Un kind qui enregistre `context_org` (le projet) range ; une page se range comme
    son projet (`governed_by`) ; les autres ne rangent pas."""
    k = _kind(resource_type)
    if k.context_org is not None:
        return True, k.context_org(resource_id)
    if k.governed_by is not None:
        parent = k.governed_by(resource_id)
        return _rangement(*parent) if parent is not None else (True, None)
    return False, None


def _perso_range_hors_de(sub: str, resource_type: str, resource_id: str,
                         owner: tuple, org_id: int) -> bool:
    """Ressource perso de `sub`, rangée ailleurs qu'en `org_id` (ou nulle part, pour un
    kind qui range) — donc invisible dans une vue bornée à `org_id`."""
    if (str(owner[0]), str(owner[1])) != ("user", sub):
        return False
    range_, org = _rangement(resource_type, resource_id)
    return range_ and (org is None or int(org) != int(org_id))


def borner_a_la_vue(sub: str, resource_type: str, items: list,
                    rid: Callable[[object], object] = lambda x: x) -> list:
    """Hors vue bornée : `items` tel quel (même objet). En vue bornée à O : ceux que
    `visible_in_org` rend visibles dans O — la règle appliquée à une LISTE, pour les
    listes qui ne passent pas par elle (partages reçus, tableaux accordés…)."""
    borne = vue_bornee()
    if borne is None:
        return items
    return [it for it in items
            if visible_in_org(sub, borne, resource_type, str(rid(it)))]


def partages_dans_la_vue(sub: str, grants: list[dict]) -> list[dict]:
    """Les partages (`resource_grants`) reçus par `sub`, bornés à la vue : hors vue,
    tels quels ; en vue bornée, seuls ceux dont la ressource est visible dans O. Un type
    de ressource hors du registre ne se prouve pas dans O : il ne passe pas."""
    borne = vue_bornee()
    if borne is None:
        return grants
    return [g for g in grants
            if g.get("resource_type") in RESOURCE_KINDS
            and visible_in_org(sub, borne, g["resource_type"], str(g["resource_id"]))]


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
    # Vue bornée (oto#270) : pas d'autre contexte que O.
    borne = vue_bornee()
    if borne is not None and (org_id is None or int(org_id) != borne):
        return False
    otype, oid = str(owner[0]), str(owner[1])
    # 1. L'org active elle-même.
    if (otype, oid) == active_owner(org_id):
        return True
    # 2. Scope MEMBRE (ADR 0030 amendé) : ma ressource perso m'est visible dans tout
    #    contexte où je suis — c'est la MIENNE. Le plan de LISTE, lui, ne la rend que
    #    dans mon org perso (`perso_de_la_liste`, décision du 28/09/2026).
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
    # rid -> org de RANGEMENT d'une ressource perso (`projects.context_org_id`) ; None si
    # le kind ne l'enregistre pas. Sert `resource_org` (org_origin, ADR 0071).
    context_org: Optional[Callable[[str], Optional[int]]] = None


#: Le type de ressource d'un tableau du datastore, **tel qu'il est ÉCRIT EN BASE**
#: (`resource_grants.resource_type`). ⚠️ Ce n'est pas un mot de vocabulaire : c'est
#: une valeur persistée, que 15 partages de production portent aujourd'hui. La
#: renommer sans migrer les lignes ferait disparaître ces partages EN SILENCE —
#: aucune erreur, juste des droits qui s'évaporent. Elle a survécu au renommage de
#: `namespace` en `datastore` pour cette raison, et elle est nommée ici pour que le
#: prochain renommage la trouve au lieu de la traverser.
TYPE_RESSOURCE_DATASTORE = "datastore_namespace"

#: Le type de ressource d'une PROCÉDURE, tel qu'il est ÉCRIT EN BASE — même régime que
#: celui du tableau ci-dessus : une valeur de ligne de `resource_grants`, pas un nom
#: servi. Le produit, lui, dit `procedure` (otomata-tech/oto#65, arbitrage du
#: 23/09/2026) : la traduction se fait à la frontière de `oto_resource`
#: (`resources_contract.KIND_OF`), et nulle part ailleurs. La valeur stockée garde le
#: mot d'avant #519 jusqu'à sa migration nommée (lot D, #526).
TYPE_RESSOURCE_PROCEDURE = "doctrine"

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
    read+write ; sinon un grant suffisant (write requis pour écrire).

    En vue bornée à O (oto#270) : d'abord visible DANS O (`visible_in_org`), puis la
    règle ordinaire — l'intersection, jamais plus large que l'une ou l'autre."""
    borne = vue_bornee()
    if borne is not None and not visible_in_org(sub, borne, resource_type, resource_id):
        return False
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
    granted_by: Optional[str] = None, ttl_days: Optional[int] = None,
):
    """Accorde un RÔLE (ADR 0048) à un principal. `role` ∈ {viewer, editor, manager}
    prime ; à défaut `permission` read/write est mappé (rétro-compat). `ttl_days` pose
    l'échéance (otomata-tech/oto#39, règle d'omission : `db.grant_resource`). Rend
    l'échéance écrite, None = sans échéance."""
    return db.grant_resource(resource_type, resource_id, principal_type, principal_id,
                             permission=permission, granted_by=granted_by, role=role,
                             ttl_days=ttl_days)


def revoke(resource_type: str, resource_id: str, principal_type: str, principal_id: str) -> bool:
    return db.revoke_resource_grant(resource_type, resource_id, principal_type, principal_id)


def list_grants(resource_type: str, resource_id: str) -> list[dict]:
    return db.list_resource_grants(resource_type, resource_id)


class GroupOutsideResourceOrg(ValueError):
    """Transfert refusé : l'équipe cible vit dans une AUTRE org que la ressource
    (servi en 403 `group_outside_resource_org` par `oto_resource`)."""


def _require_group_in_resource_org(resource_type: str, resource_id: str,
                                   group_id: str) -> None:
    """Une équipe est un cloisonnement DANS une org (ADR 0049) : y ranger une ressource
    ne la fait pas changer d'org. Un acteur membre de deux orgs pouvait ranger un projet
    de l'org A dans une équipe de l'org B. L'org de la ressource se lit sur son
    propriétaire (`org_origin.org_of`) ; inconnue (tableau perso, projet perso sans org
    de rangement) ou nulle (plateforme) → rien à comparer. Changer d'org reste possible,
    et explicite : un transfert vers l'org (`new_owner_org`)."""
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        raise ValueError(f"{resource_type} #{resource_id} introuvable")
    k = _kind(resource_type)
    groups = org_origin.group_orgs([owner, ("group", group_id)])
    target_org = groups.get(int(group_id))
    if target_org is None:
        raise ValueError(f"équipe #{group_id} inconnue")
    org, known = org_origin.org_of(
        owner[0], owner[1], groups=groups,
        context_org_id=k.context_org(resource_id) if k.context_org else None)
    if known and org is not None and org != target_org:
        raise GroupOutsideResourceOrg(
            f"l'équipe #{group_id} appartient à l'org #{target_org}, la ressource vit "
            f"dans l'org #{org} : une équipe ne fait pas changer d'org. Choisis une "
            f"équipe de l'org #{org}, ou transfère d'abord vers l'org cible "
            f"(`new_owner_org`).")


def transfer(
    resource_type: str, resource_id: str, new_owner_type: str, new_owner_id: str,
) -> None:
    """Re-parente la ressource. Préserve l'UX non-destructive : l'ancien propriétaire
    **user** garde un accès `write` (passe en partagé) ; le nouveau propriétaire
    perd son éventuel grant (il est désormais owner). Vers une équipe : elle doit être
    de l'org de la ressource (`GroupOutsideResourceOrg`) — gardé ICI, donc aussi pour
    la cascade d'un projet livré."""
    if new_owner_type == "group":
        _require_group_in_resource_org(resource_type, resource_id, new_owner_id)
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

    Le contexte ne décide plus où le projet est LISTÉ (un perso se liste dans l'org
    perso, décision du 28/09/2026) ; il décide sous quelle org le projet TRAVAILLE —
    les clés que résout son axe `project=` (`access/heritage`) et l'org d'origine
    affichée (`org_origin`). On garde le projet là où il travaillait déjà (org
    détentrice, org du groupe détenteur, ou contexte courant) si le destinataire y est
    membre ; sinon on retombe sur SON org perso — jamais NULL."""
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


def _project_context_org_id(rid: str) -> Optional[int]:
    row = db.get_project_by_id(int(rid))
    return int(row["context_org_id"]) if row and row.get("context_org_id") else None


register_kind(
    "project",
    ResourceKind(owner_getter=_project_owner, reparent=_project_reparent,
                 context_org=_project_context_org_id),
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
    TYPE_RESSOURCE_PROCEDURE,
    ResourceKind(owner_getter=_guide_owner, reparent=_guide_reparent),
)
