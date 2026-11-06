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


def vue_bornee(sub: Optional[str] = None) -> Optional[int]:
    """L'org O d'une vue bornée, sinon None. Deux sources, une seule règle :

    - une vue « en tant que » posée par un org_admin (oto#270), par `ViewAsMiddleware`
      après ses gardes ;
    - le jeton de DÉLÉGATION de `sub` (`verrou_org.py`) : son travail appartient à une
      org, et le contenu possédé par la personne n'y est visible que comme dans une vue
      bornée à cette org — un projet perso rangé dans une AUTRE org n'est ni lisible ni
      modifiable sous ce jeton. Le porteur seul : un tiers n'est jamais concerné, et un
      appel sans `sub` ne lit que la vue « en tant que ».

    Toute fonction de ce seam qui la lit garde, quand elle vaut None, son chemin d'avant
    À L'IDENTIQUE. Un travail SANS org (`org_id` None) ne borne rien ici : ses rôles
    d'org et son org d'appel sont déjà refusés par le verrou lui-même."""
    vue = session_org.current_view_as_bound_org()
    if vue is not None or sub is None:
        return vue
    from . import verrou_org
    v = verrou_org.borne(sub, route="vue_bornee", ecart=True)
    return v.org_id if v is not None else None


def accessor_scope(sub: str) -> AccessorScope:
    borne = vue_bornee(sub)
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


def owner_de_portee(sub: Optional[str], org_id: Optional[int]) -> Optional[tuple[str, str]]:
    """`active_owner` de l'org, si `sub` en est un PRINCIPAL dans ce contexte — None pour
    un bénéficiaire HORS de l'org qui y travaille par un projet partagé (`_project=`,
    `access.heritage.org_du_perimetre`). Source unique de la pièce `("org", X)` des
    seams de portée : `active_org_principals` (donc `principaux_de_liste` et
    `visible_in_org`), `project_scope_owners`, `owner_in_scope`. Sans verdict de
    projet partagé (tout appel hors `_project=`), c'est `active_owner` à l'identique."""
    from .access import heritage
    return active_owner(heritage.org_du_perimetre(sub, org_id))


def active_org_principals(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """Principals du CONTEXTE de l'org active sous lesquels une ressource est visible
    ici : l'org active, l'acteur, et ses groupes DANS cette org. Le plan de l'ACCÈS
    par id (`visible_in_org`, ADR 0023) ; les LISTES en retirent l'acteur hors de sa
    maison (`principaux_de_liste`, décision du 28/09/2026). Un bénéficiaire HORS de
    l'org (projet partagé) n'y a pas l'org pour principal (`owner_de_portee`)."""
    if org_id is None:
        return []
    owner = owner_de_portee(sub, org_id)
    return ([owner] if owner is not None else []) + [("user", sub)] + [
        ("group", str(g["group_id"]))
        for g in group_store.list_groups_for_user(sub, org_id)]


def org_perso_de(sub: str, org_id: Optional[int]) -> bool:
    """`org_id` est-elle la MAISON de `sub` — son org active (`org_store.home_org`) ?
    Plus d'org perso depuis le 05/11/2026 : la maison de ce qui n'a
    pas d'org de création (`perso_de_la_liste`, le `sans_org` de `mes_objets_ici`) et
    des partages faits à la personne est l'org où elle travaille. Elle suit donc la
    bascule d'org active — c'est voulu. Le nom est resté pour ses appelants."""
    return org_id is not None and org_store.home_org(sub) == int(org_id)


def perso_de_la_liste(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """`[("user", sub)]` dans la maison de `sub` (son org active), `[]` partout ailleurs.

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


def proprietaires_collectifs_de_liste(sub: str, org_id: Optional[int]
                                      ) -> list[tuple[str, str]]:
    """Les propriétaires COLLECTIFS (l'org, mes équipes en elle) dont une liste rend les
    objets POSSÉDÉS. Jamais `("user", sub)` : mes objets perso passent par
    `mes_objets_ici`, qui les range dans leur org de création. Le principal personnel
    de `principaux_de_liste` (maison) ne sert qu'aux PARTAGES reçus — pris comme
    propriétaire, il rendait dans la maison tous mes tableaux perso, d'où qu'ils
    viennent (règle retirée le 07/10/2026, cf. `mes_objets_ici`)."""
    return [p for p in principaux_de_liste(sub, org_id) if p[0] != "user"]


def project_scope_owners(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """Owners du CONTEXTE projet de l'org active (lot 3 Ship 1, factorisation du
    scoping d'`oto_project op=list`) : l'org active + ses pôles (ADR 0049 — mes
    équipes, ou TOUTES si org_admin, même règle que `can_read_group`). Parité
    gardée par tripwire (`test_search_scope_tripwire`). Un bénéficiaire HORS de l'org
    (projet partagé) n'y a pas l'org (`owner_de_portee`)."""
    if org_id is None:
        return []
    owner = owner_de_portee(sub, org_id)
    if roles.is_org_admin(sub, int(org_id)):        # type: ignore[arg-type]
        gids = [int(g["id"]) for g in group_store.list_groups(int(org_id))]  # type: ignore[arg-type]
    else:
        gids = [int(g["group_id"]) for g in group_store.list_groups_for_user(sub, org_id)]
    return ([owner] if owner is not None else []) + [("group", str(g)) for g in gids]


def project_list_owners(sub: str, org_id: Optional[int]) -> list[tuple[str, str]]:
    """Propriétaires COLLECTIFS des projets qu'une LISTE rend dans l'org `org_id` : ceux
    du contexte (`project_scope_owners` — l'org et ses pôles). Mes projets PERSONNELS
    n'y sont pas : ils passent par `mes_objets_ici`, qui les range dans l'org où je
    les ai créés. Source unique d'`op=list`, `op=list_templates`, `archived=true`, du
    rail et de la recherche (`projets_possedes_ici`)."""
    return project_scope_owners(sub, org_id)


def mes_objets_ici(sub: Optional[str], org_id: Optional[int]
                   ) -> Optional[tuple[str, int, bool]]:
    """`(sub, org, sans_org)` : les objets PERSONNELS (`owner_type='user'`, projets et
    tableaux) que `sub` voit dans la liste de l'org `org_id` : ceux qu'il y a CRÉÉS
    (`context_org_id = org`) et, dans sa maison (`sans_org`), ceux qui ne portent
    AUCUNE org de création (`context_org_id` nul : legacy, ou créés hors de toute org).
    `None` : aucun.

    Décisions d'Alexis du 29/09/2026 : un objet appartient à qui le crée et vit dans
    l'org où il l'a créé ; son propriétaire l'y voit TOUJOURS, que l'org porte
    l'étiquette perso ou non — « perso » n'est qu'une étiquette, fonctionnellement une
    org comme une autre. Les autres membres ne le voient que s'il le leur partage : la
    clause ne porte que sur `sub`.

    Décision d'Alexis du 07/10/2026 : « l'affichage s'en tient à l'org consultée, on
    ne mélange pas ». La maison ne liste plus les objets perso créés AILLEURS (elle
    les listait tous depuis le 28/09, et l'on croyait qu'ils y vivaient) : elle ne
    garde que ceux qui n'ont d'org de création nulle part, faute de quoi ils ne
    sortiraient dans aucune liste. En vue bornée (oto#270), seul le contexte O compte."""
    if not sub or org_id is None:
        return None
    sans_org = vue_bornee(sub) is None and org_perso_de(sub, org_id)
    return (sub, int(org_id), sans_org)


def mes_tableaux_ici(createur: Optional[tuple[str, int, bool]]) -> list[dict]:
    """Mes tableaux PERSONNELS que la liste de l'org rend, selon `mes_objets_ici` : ceux
    créés dans cette org (`context_org_id`) et, dans ma maison (`sans_org`), ceux
    qui n'ont pas d'org de création. Source unique de la liste des tableaux et de la
    recherche (parité « cherchable ⇔ lisible »). `[]` sans `createur`."""
    if createur is None:
        return []
    csub, corg, sans_org = createur
    return [n for n in db.list_datastores_for_owners([("user", csub)])
            if n.get("context_org_id") == corg
            or (sans_org and n.get("context_org_id") is None)]


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


def org_de_rangement(row: dict) -> Optional[int]:
    """L'org où une ressource possédée est RANGÉE, lue sur sa ligne (`owner_type`,
    `owner_id`, `context_org_id`) : l'org propriétaire, l'org de l'équipe propriétaire,
    l'org de création d'un objet perso. None quand la ligne n'en dit rien (objet perso
    sans org de création, plateforme). Vaut pour les projets, tableaux et apps."""
    ot, oid = row.get("owner_type"), row.get("owner_id")
    if ot == "org" and oid is not None:
        return int(oid)
    if ot == "group" and oid is not None:
        g = group_store.get_group(int(oid))
        return int(g["org_id"]) if g and g.get("org_id") is not None else None
    if ot == "user" and row.get("context_org_id") is not None:
        return int(row["context_org_id"])
    return None


def partages_a_moi_ici(sub: str, org_id: Optional[int], rows: list[dict],
                       rang: Callable[[dict], Optional[int]] = org_de_rangement
                       ) -> list[dict]:
    """Parmi `rows`, des ressources partagées à `sub` EN PERSONNE, celles qu'une liste
    rend dans l'org `org_id` (06/11/2026).

    Un partage nominatif se range là où la ressource est rangée (`rang`, par défaut
    `org_de_rangement`) quand `sub` est membre de cette org : il se liste là, et nulle
    part ailleurs. La MAISON (`org_perso_de`) ne recueille que ce qui n'a pas d'org où
    le ranger pour lui : une ressource sans org de rangement, ou celle d'une org dont
    il n'est pas membre (le collaborateur extérieur). Avant, tout partage nominatif
    allait à la maison, qui depuis la fin de l'org perso est une org ordinaire : la
    ressource d'une org A partagée à un membre de A et de B se listait dans B, son org
    active.

    Un filtre de LISTE, jamais un droit : la lecture par id (`visible_in_org`) ne
    change pas. Source unique des projets, tableaux, pages et apps reçus."""
    if org_id is None or not rows:
        return []
    maison = org_perso_de(sub, org_id)
    mes_orgs = {int(o["org_id"]) for o in org_store.list_orgs_for_user(sub)}
    out = []
    for r in rows:
        o = rang(r)
        # Rangé ici ET membre d'ici — un bénéficiaire HORS de l'org qui y travaille par
        # un projet partagé (`_project=`) n'en reçoit rien de plus que sa maison.
        if (o == int(org_id) and o in mes_orgs) or (
                maison and (o is None or o not in mes_orgs)):
            out.append(r)
    return out


def projets_partages_a_moi_ici(sub: str, org_id: Optional[int]) -> list[dict]:
    """Les projets partagés à `sub` en personne qu'une liste rend dans l'org `org_id`
    (`partages_a_moi_ici`)."""
    if org_id is None:
        return []
    return partages_a_moi_ici(sub, org_id, db.list_projects_granted_to([("user", sub)]))


def projets_recus_ici(sub: str, org_id: Optional[int]) -> list[dict]:
    """Les projets PARTAGÉS qu'une liste rend dans l'org `org_id` : partagés à l'org ou
    à mes équipes en elle (`proprietaires_collectifs_de_liste`), plus mes partages
    nominatifs rangés ici (`projets_partages_a_moi_ici`). Une ligne par projet, la
    meilleure `permission`. Source unique d'`oto_project op=list` et
    d'`accessible_project_ids` — « cherchable ⇔ lisible »."""
    par_id: dict[int, dict] = {}
    for r in (db.list_projects_granted_to(proprietaires_collectifs_de_liste(sub, org_id))
              + projets_partages_a_moi_ici(sub, org_id)):
        rid = int(r["id"])
        prev = par_id.get(rid)
        if prev is None or (r.get("permission") == "write"
                            and prev.get("permission") != "write"):
            par_id[rid] = r
    return sorted(par_id.values(), key=lambda r: str(r.get("updated_at") or ""),
                  reverse=True)


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
    for r in projets_recus_ici(sub, org_id):
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
      c'est O. Le projet et le tableau rangent (org de création ; un tableau sans
      org de création se range dans la maison de son propriétaire) ; un kind qui ne
      range pas ses ressources perso (procédure personnelle) les fait suivre la
      personne partout, O compris ;
    - un partage PERSONNEL reçu (`principal = user`) ne compte pas : il n'appartient à
      aucune org, c'est l'espace du membre, pas celui de O."""
    borne = vue_bornee(sub)
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
    borne = vue_bornee(sub)
    if borne is None:
        return items
    return [it for it in items
            if visible_in_org(sub, borne, resource_type, str(rid(it)))]


def partages_dans_la_vue(sub: str, grants: list[dict]) -> list[dict]:
    """Les partages (`resource_grants`) reçus par `sub`, bornés à la vue : hors vue,
    tels quels ; en vue bornée, seuls ceux dont la ressource est visible dans O. Un type
    de ressource hors du registre ne se prouve pas dans O : il ne passe pas."""
    borne = vue_bornee(sub)
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
    borne = vue_bornee(sub)
    if borne is not None and (org_id is None or int(org_id) != borne):
        return False
    otype, oid = str(owner[0]), str(owner[1])
    # 1. L'org active elle-même — sauf pour un bénéficiaire HORS d'elle (projet partagé).
    if (otype, oid) == owner_de_portee(sub, org_id):
        return True
    # 2. Scope MEMBRE (ADR 0030 amendé) : ma ressource perso m'est visible dans tout
    #    contexte où je suis — c'est la MIENNE. Le plan de LISTE, lui, ne la rend que
    #    dans ma maison (`perso_de_la_liste`, décision du 28/09/2026).
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


def org_ou_vit(resource_type: str, resource_id: str) -> "org_origin.Origine":
    """L'org où VIT une ressource (`org_origin.org_of`) : son propriétaire, et pour une
    ressource perso l'org où son kind la range (`context_org`). Ressource inconnue :
    `(None, False)` — rien n'est deviné."""
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        return None, False
    k = _kind(resource_type)
    return org_origin.org_of(
        owner[0], owner[1], groups=org_origin.group_orgs([owner]),
        context_org_id=k.context_org(resource_id) if k.context_org else None)


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
    borne = vue_bornee(sub)
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


def can_access_many(sub: str, resource_type: str, owners: dict[str, tuple[str, str]],
                    want: str = "read") -> dict[str, bool]:
    """`can_access` pour une LISTE dont le propriétaire est DÉJÀ lu : `owners` =
    `{resource_id: (owner_type, owner_id)}`, rend `{resource_id: bool}`. Même règle,
    dans le même ordre ; ce qui ne dépend que du propriétaire se décide une fois par
    propriétaire distinct, et les partages se lisent en une requête.

    ⚠️ Pourquoi elle existe (incident du 13/10/2026) : la liste des projets appelait
    `can_access` projet par projet — trois requêtes chacun, dont la relecture d'une
    ligne qu'elle venait de lire. En vue bornée, la visibilité dans O reste vérifiée
    ressource par ressource (`visible_in_org`) : c'est la vue d'un admin, pas le chemin
    courant."""
    borne = vue_bornee(sub)
    out: dict[str, bool] = {}
    par_proprio: dict[tuple[str, str], bool] = {}
    a_lire: list[str] = []
    for rid, owner in owners.items():
        rid = str(rid)
        if borne is not None and not visible_in_org(sub, borne, resource_type, rid):
            out[rid] = False
            continue
        if owner[0] == "platform" and want == "read":
            out[rid] = True
            continue
        cle = (str(owner[0]), str(owner[1]))
        if cle not in par_proprio:
            par_proprio[cle] = _owner_match_content(sub, *cle)
        if par_proprio[cle]:
            out[rid] = True
        else:
            a_lire.append(rid)
    if a_lire:
        best = db.best_resource_grants(resource_type, a_lire,
                                       accessor_scope(sub).principal_pairs())
        for rid in a_lire:
            out[rid] = rid in best and (want == "read" or best[rid] == "write")
    return out


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


def _gouverne(owner: Optional[tuple], transfere: Callable[[], bool],
              gerant: Callable[[], bool]) -> bool:
    """LA règle de gouvernance d'une ressource sans parent gouvernant (ADR 0048), écrite
    une seule fois : un propriétaire, puis l'escalade structurelle (`transfere`, la règle
    de `_can_transfer_owner`), puis un grant `gérant` VIVANT de l'un des principals de
    l'acteur (`gerant`). `can_govern` (une ressource) et `can_govern_many` (une liste)
    la servent toutes deux ; elles ne diffèrent que par la façon de LIRE ces deux faits."""
    return owner is not None and (transfere() or gerant())


def can_govern(sub: str, resource_type: str, resource_id: str) -> bool:
    """Plan GOUVERNANCE (ADR 0048) : re-partager / révoquer / supprimer / publier, SANS
    lire le contenu. **Grantable** = owner ∪ **grant `gérant`** ∪ escalade `roles.py`. Le
    transfert de propriété, lui, exclut le gérant → `can_transfer`. Pour une LISTE dont
    le propriétaire est déjà lu : `can_govern_many`, même règle (`_gouverne`)."""
    if (k := RESOURCE_KINDS.get(resource_type)) and k.governed_by:  # une page → son projet
        return (cible := k.governed_by(resource_id)) is not None and can_govern(sub, *cible)
    return _gouverne(owner_of(resource_type, resource_id),
                     lambda: can_transfer(sub, resource_type, resource_id),
                     lambda: _has_manager_grant(sub, resource_type, resource_id))


def can_govern_many(sub: str, resource_type: str,
                    owners: dict[str, Optional[tuple[str, str]]]) -> dict[str, bool]:
    """`can_govern` pour une LISTE dont le propriétaire est DÉJÀ lu : `owners` =
    `{resource_id: (owner_type, owner_id) | None}` (None = sans propriétaire, comme
    `owner_of`), rend `{resource_id: bool}`. Même règle (`_gouverne`), mêmes faits :
    l'escalade se juge une fois par propriétaire DISTINCT (`_can_transfer_owner`, la
    fonction même de `can_transfer`), et les grants `gérant` de tous les principals de
    l'acteur (`accessor_scope`, celui de `_has_manager_grant`) se lisent en UNE requête
    pour toute la liste (`db.manager_resource_grants`), au lieu d'une par principal et
    par ressource.

    ⚠️ Pourquoi elle existe : `GET /api/datastores` appelait `can_govern` tableau par
    tableau — le propriétaire relu deux fois, l'escalade, puis une lecture par principal,
    soit 5 à 10 emprunts au pool par tableau (`docs/event-loop-perf.md`).

    Un kind gouverné par son parent (`governed_by`, une page → son projet) n'a pas de
    propriétaire à lui : il LÈVE ici, plutôt que de juger une ressource sur le mauvais
    propriétaire."""
    k = _kind(resource_type)
    if k.governed_by is not None:
        raise ValueError(f"`{resource_type}` se gouverne par son parent : `can_govern`")
    par_proprio: dict[tuple, bool] = {}

    def transfere(owner: tuple) -> bool:
        cle = (owner[0], owner[1])
        if cle not in par_proprio:
            par_proprio[cle] = _can_transfer_owner(sub, owner[0], owner[1])
        return par_proprio[cle]

    a_lire = [str(rid) for rid, o in owners.items() if o is not None and not transfere(o)]
    gerants = (db.manager_resource_grants(resource_type, a_lire,
                                          accessor_scope(sub).principal_pairs())
               if a_lire else set())
    return {str(rid): _gouverne(o, lambda o=o: transfere(o),
                                lambda rid=rid: str(rid) in gerants)
            for rid, o in owners.items()}


def proprietaire_de_ligne(row: dict) -> Optional[tuple[str, str]]:
    """Le propriétaire d'une ligne de tableau DÉJÀ lue (`owner_type`/`owner_id`), sous la
    forme et la règle de `owner_of` : None sans `owner_id`. Source unique de
    `_datastore_owner`, et de la liste qui juge sa gouvernance par lot."""
    if row.get("owner_id") is None:
        return None
    return (row["owner_type"], row["owner_id"])


def can_publish(sub: str, resource_type: str, resource_id: str) -> bool:
    """Plan PUBLICATION (#1176) : rendre une ressource lisible SANS LOGIN (endpoint MCP
    publié d'un projet, page ou fichier public) — l'appartenance au PROPRIÉTAIRE, en plus
    du droit que le geste exige déjà (`can_govern` pour un endpoint, `write` pour une
    page ou un fichier). Elle ne le remplace pas : ce prédicat ne dit rien du rôle.

    **Pourquoi pas `can_govern`.** Le gérant (`manager`) est grantable à quiconque, membre
    ou non. Or l'endpoint publié résout clés et quota SOUS L'ORG PROPRIÉTAIRE : un gérant
    extérieur ouvrait au public l'usage des clés d'une org dont il ne fait pas partie.

    - projet d'org → membre de cette org ;
    - projet d'équipe → membre de l'org PARENTE de l'équipe (un membre de l'org atteint
      déjà ses clés ; l'équipe ne restreint pas la publication) ;
    - projet personnel → son propriétaire, et lui seul ;
    - projet plateforme → personne d'autre que l'escalade ;
    - escalade plateforme (`roles.is_platform_admin`) → toujours, comme partout ici.

    Une page se publie sous la règle de son projet (`governed_by`)."""
    if (k := RESOURCE_KINDS.get(resource_type)) and k.governed_by:  # une page → son projet
        return (cible := k.governed_by(resource_id)) is not None and can_publish(sub, *cible)
    owner = owner_of(resource_type, resource_id)
    if owner is None:
        return False
    if roles.is_platform_admin(sub):
        return True
    owner_type, owner_id = owner
    if owner_type == "user":
        return sub == owner_id
    if owner_type == "org":
        return roles.is_org_member(sub, int(owner_id))
    if owner_type == "group":
        g = group_store.get_group(int(owner_id))
        return bool(g and g.get("org_id") is not None
                    and roles.is_org_member(sub, int(g["org_id"])))
    return False


# --- Mutations ----------------------------------------------------------------

#: Les kinds dont un partage nominatif se liste dans l'org de la ressource, pas dans la
#: maison du destinataire : un agent se partage entre membres de l'org où il tourne
#: (`capabilities/_acces_agent.lister`). Un tel partage ne pose pas de maison.
_PARTAGES_LISTES_DANS_L_ORG = frozenset({"runner_trigger"})


def maison_du_partage(resource_type: str, principal_type: str, principal_id: str
                      ) -> Optional[int]:
    """Garantit au destinataire PERSONNEL d'un partage la maison où il le retrouvera :
    son org active (`maison_de`). Sans elle, le partage ne sortirait dans aucune
    liste. Rend la maison, ou None quand le partage n'en demande pas (équipe, org,
    kind listé dans l'org de la ressource). Un échec LÈVE : le partage n'est pas posé
    sans sa maison."""
    if principal_type != "user" or resource_type in _PARTAGES_LISTES_DANS_L_ORG:
        return None
    return maison_de(principal_id)


def maison_de(sub: str) -> int:
    """La MAISON de `sub` (`org_perso_de`) : son org active, posée s'il n'en a aucune
    (`ensure_home_org`, une org ordinaire). Le geste de ce qui ne se range que là : un
    partage nominatif reçu (`maison_du_partage`) et un objet personnel créé sans org de
    création — guide personnel, procédure personnelle, nœud hors projet. Une lecture
    quand la maison existe ; un échec LÈVE : l'objet n'est pas posé sans sa maison."""
    pid = org_store.home_org(sub)
    if pid is not None:
        return pid
    u = db.get_user(sub)
    if u is None:
        # Un compte sans sa ligne `users` naît D'ABORD : `ensure_home_org` la ferait
        # naître au milieu de sa propre création (`add_org_member` → `upsert_user`),
        # dont l'effet de naissance pose déjà une maison — deux maisons.
        db.upsert_user(sub)
        pid = org_store.home_org(sub)
        if pid is not None:
            return pid
        u = {}
    return org_store.ensure_home_org(sub, email=u.get("email"), name=u.get("name"))


def grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
    permission: Optional[str] = None, *, role: Optional[str] = None,
    granted_by: Optional[str] = None, ttl_days: Optional[int] = None,
):
    """Accorde un RÔLE (ADR 0048) à un principal. `role` ∈ {viewer, editor, manager}
    prime ; à défaut `permission` read/write est mappé (rétro-compat). `ttl_days` pose
    l'échéance (otomata-tech/oto#39, règle d'omission : `db.grant_resource`). Rend
    l'échéance écrite, None = sans échéance. Un partage à une PERSONNE lui garantit
    d'abord sa maison (`maison_du_partage`)."""
    maison_du_partage(resource_type, principal_type, principal_id)
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
        maison_du_partage(resource_type, "user", prev[1])
        db.grant_resource(resource_type, resource_id, "user", prev[1], "write")


# --- Enregistrement du kind `datastore_namespace` (pilote ADR 0030) ----------

def _datastore_owner(rid: str) -> Optional[tuple[str, str]]:
    row = db.get_datastore_owner(int(rid))
    return None if row is None else proprietaire_de_ligne(row)


def _datastore_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    db.reparent_datastore(int(rid), new_owner_type, new_owner_id)


def _datastore_context_org(rid: str) -> Optional[int]:
    """Org de RANGEMENT d'un tableau perso : celle où il a été créé (`context_org_id`,
    oto#160). Sans org de création (legacy), sa maison est l'org active de son
    propriétaire — le critère même de la liste (`mes_tableaux_ici`). Sans ce rangement,
    un tableau perso « suivait la personne partout » : en vue bornée à O, un tableau
    créé dans une autre org se lisait par son numéro."""
    row = db.get_datastore_by_id(int(rid))
    if row is None:
        return None
    if row.get("context_org_id"):
        return int(row["context_org_id"])
    if row.get("owner_type") == "user" and row.get("owner_id"):
        return org_store.home_org(str(row["owner_id"]))
    return None


register_kind(
    TYPE_RESSOURCE_DATASTORE,
    ResourceKind(owner_getter=_datastore_owner, reparent=_datastore_reparent,
                 context_org=_datastore_context_org),
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
    membre ; sinon on retombe sur SA maison (org active) — jamais NULL."""
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
    return org_store.ensure_home_org(new_owner_id)


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
