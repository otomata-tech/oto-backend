"""Ouvrir UN nœud : sa fiche, son fil, son corps en blocs — ou le schéma d'un tableau.

Deuxième surface de lecture précoce du modèle de nœuds, après `/shell`. Même régime :
LECTURE seule, forme contractée avec le front (`/nodes/{nodeId}`) et **déclarée
provisoire**.

**404 indistinct, et c'est le point de sécurité de ce module.** Un nœud inexistant et
un nœud interdit rendent la MÊME réponse : un 403 dirait « il existe, mais pas pour
toi » — donc révélerait l'existence d'une page à qui n'y a pas droit, et permettrait
d'énumérer le contenu d'une org en lisant les codes d'état. Le front l'a écrit dans son
propre contrat ; on l'applique parce que c'est juste, pas parce qu'il le demande.

⚠️ **Ouvrir un TABLEAU rend son schéma de colonnes, jamais ses lignes.** Les lignes ont
leur surface, paginée par curseur. Un « ouvrir » qui ramène 43 584 lignes n'est pas une
fiche, c'est un export déguisé — et c'est la panne qui ressemblerait à un problème de
performance plutôt qu'à un problème de modèle.

**Quatre écarts au contrat du front, tous assumés et nommés** (arbitrés le 17/08) :

1. ✅ **Les blocs sont ÉTIQUETÉS depuis le 21/08** (lot ⑦). ⚠️ Ce paragraphe a dit le
   contraire jusque-là — « nos blocs sont trop gros, un titre et trois paragraphes
   partagent un id » — et **c'était faux** : mesuré sur 140 blocs de corps réels, le
   découpage se fait aux lignes vides ET aux titres, zéro bloc mixte, médiane 175 c.
   L'erreur venait d'une lecture partielle du parse (la fonction qui découpe le texte
   n'avait pas été lue), corrigée par la mesure. Ce qui manquait n'était pas le grain
   mais **l'étiquette** : `role` (`heading`/`paragraph`/`list`) et `items[]` sont
   désormais servis, en PROPRIÉTÉ et jamais en `type` (0054-D2).
   **L'interdit d'ancrage sur un `blk_*` est LEVÉ** : l'étiquetage n'a fait tourner
   aucune identité, la clé de rapprochement étant passée sur la SOURCE SEULE.
2. **`modified` est servi en ISO, pas en « jeudi ».** Le front demande la date
   humanisée ; elle n'a pas sa place dans une réponse CACHÉE et versionnée : « jeudi »
   devient faux la semaine suivante sans que `rev` bouge, donc le 304 confirme un cache
   qui ment. Leur propre règle — « le back n'écrit pas les libellés de l'interface » —
   plaide dans le même sens.
3. **`access` et `dependencies` sont ABSENTS, pas vides.** Aucune source de nœud ne les
   porte encore. Un `editors: []` affirmerait « personne d'autre » ; un
   `dependencies: []` autoriserait une suppression. Absent dit « on ne sait pas encore ».
4. **`trail` est servi**, borné en profondeur, avec la fratrie de chaque maillon.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import ownership
from ..db import blocks as db_blocks
from ..db import node_view as db_node
from ..db import shell as db_shell
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, NotModified, ResolvedCtx, RestBinding
from .node_keys import (EditSurface, NoeudIncoherent, doc_id_de, edit_surface_de,
                        exiger_poignee)
from .node_procedure_ref import ProcedureRef, procedure_ref_of
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_PROFONDEUR_FIL = 12
_FRERES_MAX = 50

_TYPE_PAR_KIND = {"page": "page", "tableau": "table"}
# Même règle qu'au rail : la nature est DÉRIVÉE d'un rôle porté en propriété, jamais
# d'un `kind` de plus. Un rôle inconnu retombe sur le genre.
_TYPE_PAR_ROLE = {"procedure": "agent"}


class NodeInput(BaseModel):
    node_id: str
    rev: Optional[str] = None


class TrailSibling(BaseModel):
    id: str
    name: str
    type: str
    # La procédure qu'un voisin `agent` exécute — même référence que sur le rail et
    # sur la fiche (#417/#619), laissée ouverte ici jusqu'au 01/09. Le fil savait dire
    # `type: "agent"` mais pas vers quoi : le voisin restait non cliquable alors que
    # les deux autres surfaces l'ouvraient. `null` sur toute autre nature, et sur un
    # agent sans référence lisible : jamais devinée.
    procedure: Optional[ProcedureRef] = None


class TrailCrumb(BaseModel):
    id: str
    name: str
    type: str
    # Les contenus rangés au même endroit, le maillon lui-même compris — le popover du
    # fil n'a donc rien à redemander. Bornés : un dossier à 4 000 frères ferait du fil
    # la plus grosse partie de la réponse.
    siblings: list[TrailSibling] = []
    # Le maillon lui-même peut être un agent (on ouvre une page rangée SOUS lui) —
    # même règle, même source.
    procedure: Optional[ProcedureRef] = None


class ContentBlock(BaseModel):
    id: str
    # Le SUPPORT du bloc — `text` | `code` (0054-D2 : + `image`, `référence` un jour).
    # Ce n'est PAS ce que l'écran rend : ça, c'est `role`.
    type: str
    # Le RÔLE DE PRÉSENTATION, propriété et jamais valeur de `type` : `heading` |
    # `paragraph` | `list`. **Absent quand on ne sait pas classer** (tableau, citation) —
    # un `paragraph` posé par défaut mentirait, et le front rend alors la source comme
    # il l'entend. Servir le rôle À CÔTÉ du type, plutôt qu'à sa place, garde `type`
    # disponible pour ce qu'il désigne : le jour où `image` arrive, il n'entre pas en
    # collision avec `heading`.
    role: Optional[str] = None
    # Les puces d'une liste, déjà extraites. Le front ne peut pas les dériver sans
    # reparser du markdown — ce serait une seconde implémentation du parse, qui
    # divergerait de la nôtre au premier cas limite.
    items: Optional[list[str]] = None
    # La source EXACTE du bloc. Invariant du parse : concaténer les `md` d'un nœud rend
    # son corps au caractère près — c'est ce qui permet au front de rendre ce qu'il sait
    # rendre et de laisser passer le reste, plutôt que de recevoir une forme appauvrie.
    md: Optional[str] = None
    lang: Optional[str] = None
    # La liste de CE bloc est-elle numérotée (`1.` / `1)`) ? Présent sur un `role: list`
    # seulement — `items[]` porte les puces mais pas leur marqueur, et le front ne
    # reparse pas `md`. Dérivé de la source à la lecture (`db/blocks.ordered_of`).
    ordered: Optional[bool] = None


class NodeModified(BaseModel):
    # ISO, jamais une phrase relative : cf. l'écart 2 de l'entête.
    at: Optional[str] = None
    by: Optional[str] = None


class NodeOut(BaseModel):
    """Un nœud ouvert : sa fiche, son fil, son corps en blocs (page) ou le schéma de
    ses colonnes (tableau).

    `doc_id` / `project_id` sont les POIGNÉES vers les autres surfaces — les entiers
    que `POST /api/me/docs` (op=backlinks, op=update) et `POST /api/resources` (op=get,
    `resource_type: "project"`, `resource_id: str(project_id)`) attendent. Une page
    porte les deux, un projet son `project_id` seul, un tableau ou une procédure le
    projet qui les range (s'il y en a un) ; un nœud sans source legacy rend `null`.
    Ajoutés le 29/08/2026 à la demande d'un front tiers : sans eux la fiche se lisait
    mais ne s'éditait ni ne se partageait.

    `rev` est l'empreinte du corps servi : elle a changé une fois pour tous les nœuds
    à l'ajout de ces champs (un cache client se rafraîchit, puis retrouve ses 304).
    **Même effet à l'ajout de `pinned` et `datastore` (01/09/2026, alors `namespace`)** — une seule vague
    d'invalidation, connue et bornée. **Et encore une fois à l'ajout d'`edit_surface`
    (13/09/2026, oto#198)** : même geste que `doc_id` et `pinned`, une rotation unique
    pour tous les nœuds. C'est le prix d'une empreinte calculée sur le
    contenu servi, et c'est le bon prix : une empreinte qui ignorerait les champs neufs
    laisserait un client sur une version qu'il croit à jour."""
    id: str
    name: str
    type: Literal["page", "table", "agent", "execution"]
    # Agent : la procédure qu'il exécute (id stable, slug, scope) — le chemin vers sa
    # fiche (#417). `null` sur toute autre nature et sur un agent sans référence
    # lisible : un `null` dit « n'en exécute aucune », un id deviné dirait faux.
    procedure: Optional[ProcedureRef] = None
    doc_id: Optional[int] = None
    project_id: Optional[int] = None
    # L'ÉPINGLE (0054-D5) : ce nœud est-il une racine ? Le drapeau est posé depuis la
    # conversion des projets et n'était **lu par personne** — dans le rail, une racine
    # était donc indiscernable d'une page ordinaire. Servi ici parce que c'est la
    # seule chose qui distingue les deux, et parce que le modèle a retiré le GENRE
    # `project` exprès : l'épingle est ce qui reste pour le dire.
    pinned: bool = False
    # OÙ ce nœud s'écrit (oto#198). Dérivé du stockage par `node_keys.edit_surface_de`, la
    # MÊME fonction que lit la garde d'écriture de `oto_node_edit`. REQUIS, sans défaut :
    # un nœud qui ne sait pas dire sa surface ne se sert pas (500 `noeud_incoherent`), et
    # l'absence de `doc_id` ne dit rien — une racine de projet ou un guide n'en ont pas.
    edit_surface: EditSurface = Field(description=(
        "La surface CANONIQUE qui écrit ce nœud : node | doc | project | procedure | "
        "datastore | guide. Pas une permission : écrire reste jugé par la garde de "
        "propriété. `guide` n'a aucune poignée sur cette fiche."))
    # Tableau : le NOM DU TABLEAU à repasser aux surfaces `data_*`.
    #
    # ⚠️ Il vaut aujourd'hui la même chose que `name`, et c'est une COÏNCIDENCE qu'on
    # dissout exprès : la projection pose `title = datastore`, mais le modèle veut que
    # l'adresse devienne une position dans l'arbre, pas un nom. Le jour où c'est
    # fait, un client qui lisait `name` comme une adresse casserait **sans que rien ne
    # le prévienne**. La poignée est donc déclarée maintenant, tant qu'elle est facile
    # à tenir — même geste que `doc_id`/`project_id`. Absent sur une page.
    #
    # Nom de la clé : `datastore` depuis le 10/09/2026 (ex-`namespace`, bascule sèche,
    # sans doublon). Cette poignée-ci était la DERNIÈRE du sens « tableau » à porter
    # l'ancien nom : elle avait échappé à la bascule parce que son module s'appelle
    # `node_view` et que le filtre triait sur le nom du module, pas sur ce que la clé
    # désigne.
    datastore: Optional[str] = Field(default=None, description=(
        "Tableau : le nom du tableau à repasser aux surfaces `data_*`. `null` sur "
        "une page. ⚠️ **C'est un NOM, et un nom peut désigner deux tableaux.** Les "
        "écritures de lignes le résolvent dans le scope de l'appelant, où « vivier », "
        "« leads » ou « contacts » existent souvent en plusieurs exemplaires (perso, "
        "équipe, org) : deux homonymes atteignables suffisent à écrire dans l'autre, "
        "sans erreur. Tant que l'écriture au grain du nœud n'existe pas, un client qui "
        "enchaîne « ouvrir ce tableau » puis « y écrire » assume cette ambiguïté."))
    trail: list[TrailCrumb] = []
    modified: NodeModified
    # Page : le corps en blocs. Absent sur un tableau.
    body: Optional[list[ContentBlock]] = None
    # Tableau : le schéma de ses colonnes. Absent sur une page — et JAMAIS les lignes.
    columns: Optional[Any] = None
    rev: str
    # Ce que cette v0 ne sait pas encore dire, DIT plutôt que deviné. Sans cette clé,
    # l'absence d'`access` se lirait comme « aucun partage » et celle de `dependencies`
    # comme « rien ne s'en sert » — deux affirmations qu'on ne peut pas tenir.
    non_servi: list[str] = []


def _type_of(kind: str, props: Optional[dict] = None) -> str:
    role = (props or {}).get("role")
    if role in _TYPE_PAR_ROLE:
        return _TYPE_PAR_ROLE[role]
    return _TYPE_PAR_KIND.get(kind or "", "page")


def _introuvable() -> AuthzDenied:
    """LE refus de ce module — un seul, pour les deux causes.

    Inexistant et interdit doivent être indiscernables : un 403 sur l'un des deux
    transforme le code d'état en oracle d'existence.
    """
    return AuthzDenied(404, "not_found", "Aucun nœud de ce nom, ou aucun droit de le voir.")


def _incoherent(public_id: str, cause: NoeudIncoherent) -> AuthzDenied:
    """Un nœud dont le stockage ne dit pas où il s'écrit (oto#198) : un refus NOMMÉ,
    jamais une fiche à poignée nulle ni un repli sur `node`.

    Journalisé, parce que c'est une donnée à expliquer et pas un geste de l'appelant — par
    l'identifiant et la cause SEULS : les props brutes d'un nœud privé n'ont rien à faire
    dans un journal. Ne se sert qu'APRÈS la garde de lecture : un tiers reçoit toujours
    le 404 indistinct.
    """
    logger.error("nœud %s incohérent : %s", public_id, cause)
    return AuthzDenied(500, "noeud_incoherent",
                       f"Le nœud {public_id} ne peut pas dire où il s'écrit : {cause}.")


def _lisible(ctx: ResolvedCtx, fiche: dict, partages: set) -> bool:
    """Le nœud est-il à portée de cette personne ?

    Deux voies : son propriétaire est à portée dans le contexte, ou le nœud m'est
    partagé en direct. La première est `ownership.owner_in_scope` — **la règle de
    portée de la plateforme, pas une seconde définition**.

    Elle l'était, et elle avait divergé (#682) : ce test comparait le propriétaire à
    `active_org_principals`, qui ne connaît ni le cran PLATEFORME ni l'escalade
    d'équipe. Un guide de la bibliothèque était donc **invisible par `oto_node` et
    lisible par `oto_resource`** — deux portes, deux réponses, pour la même page et la
    même personne ; et un administrateur d'org voyait les tableaux de toutes ses
    équipes mais pas leurs pages. Le commentaire d'origine annonçait le risque
    (« une seconde définition divergerait de la première ») en le prenant quand même :
    réutiliser le calcul des principals n'est pas réutiliser la règle.
    """
    if ownership.owner_in_scope(ctx.sub, ctx.org_id,
                                (fiche["owner_type"], str(fiche["owner_id"]))):
        return True
    return fiche["public_id"] in partages


def _source(props: dict, chaine: list[dict]) -> tuple[Optional[int], Optional[int]]:
    """`(doc_id, project_id)` — la clé legacy du nœud, rendue aux surfaces qui la
    prennent en entrée (cf. `NodeOut`).

    Un nœud converti garde sa source dans `props.legacy` / `legacy_id` : une page est
    un `doc`, un projet un `prj` (`db/nodes.py`). Le projet d'une page est dans ses
    props ; celui d'un tableau ou d'une procédure rangés sous un projet se lit sur le
    FIL — le maillon `prj` le plus proche, du nœud vers la racine. Le fil est borné
    (`_PROFONDEUR_FIL`) : au-delà, le projet n'est pas trouvé et on rend `null` plutôt
    qu'un entier deviné.
    """
    legacy, lid = props.get("legacy"), props.get("legacy_id")
    doc_id = doc_id_de(legacy, lid)
    if legacy == "prj" and lid is not None:
        return doc_id, int(lid)
    if props.get("project_id") is not None:
        return doc_id, int(props["project_id"])
    for maillon in reversed(chaine):
        p = maillon.get("props") or {}
        if p.get("legacy") == "prj" and p.get("legacy_id") is not None:
            return doc_id, int(p["legacy_id"])
    return doc_id, None


def _bloc(b: dict) -> dict:
    props = b.get("props") or {}
    role = props.get("role")
    return ContentBlock(
        id=b["public_id"], type=b["type"], role=role, items=props.get("items"),
        md=props.get("md"), lang=props.get("lang"),
        ordered=db_blocks.ordered_of(props.get("md")) if role == "list" else None,
    ).model_dump(exclude_none=True)


def _fil(fiche: dict, chaine: list[dict]) -> list[TrailCrumb]:
    """Le chemin racine→nœud, avec la fratrie de chaque maillon."""
    if not chaine:
        return []
    freres = db_node.siblings_of([c["parent_id"] for c in chaine],
                                 owner=(fiche["owner_type"], str(fiche["owner_id"])),
                                 cap=_FRERES_MAX)
    # Le `scope` d'une référence est le PROPRIÉTAIRE du nœud, et le fil ne remonte que
    # des ancêtres — la fratrie est d'ailleurs bornée à ce même propriétaire par la
    # requête. Celui de la fiche vaut donc pour tous les maillons ; le relire par nœud
    # coûterait une colonne de plus pour la même valeur.
    proprietaire = fiche.get("owner_type")
    out = []
    for c in chaine:
        props = c.get("props") or {}
        nature = _type_of(c["kind"], props)
        out.append(TrailCrumb(
            id=c["public_id"], name=props.get("title") or "", type=nature,
            procedure=procedure_ref_of(nature, proprietaire, props),
            siblings=[TrailSibling(id=s["public_id"], name=s.get("title") or "",
                                   type=_type_of(s["kind"], s),
                                   procedure=procedure_ref_of(
                                       _type_of(s["kind"], s), proprietaire, s))
                      for s in freres.get(c["parent_id"], [])]))
    return out


def _rev(corps: dict) -> str:
    """Empreinte du corps canonique — même règle qu'au rail (contenu, pas horodatage)."""
    return hashlib.sha256(
        json.dumps(corps, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:32]


def _compose(ctx: ResolvedCtx, node_id: str) -> dict:
    """Tout le travail SYNCHRONE. Appelé hors boucle (cf. `_node`)."""
    fiche = db_node.node_by_public_id(node_id)
    if not fiche:
        raise _introuvable()

    partages: set = set()
    if not ownership.owner_in_scope(ctx.sub, ctx.org_id,
                                    (fiche["owner_type"], str(fiche["owner_id"]))):
        # Le second chemin ne se paie QUE s'il sert : la voie du propriétaire couvre la
        # quasi-totalité des ouvertures, et lire tous les grants d'une personne pour
        # confirmer ce qu'on sait déjà serait une requête par ouverture de page.
        par_id, _ = db_shell.resolve_grant_nodes(db_shell.direct_grants(ctx.sub))
        partages = set(par_id)
    if not _lisible(ctx, fiche, partages):
        raise _introuvable()

    props = fiche.get("props") or {}
    # APRÈS la garde de lecture : un nœud incohérent reste un 404 pour qui ne le lit pas.
    # Même dérivation que la garde d'écriture de `node_edit` — la fiche annonce ce que
    # l'écriture accepte, pas une seconde lecture du stockage.
    try:
        surface = edit_surface_de(props)
    except NoeudIncoherent as cause:
        raise _incoherent(fiche["public_id"], cause) from None
    nature = _type_of(fiche["kind"], props)
    ref = procedure_ref_of(nature, fiche.get("owner_type"), props)
    chaine = db_node.ancestors_of(fiche["id"], max_depth=_PROFONDEUR_FIL)
    doc_id, project_id = _source(props, chaine)
    corps: dict = {
        "id": fiche["public_id"],
        "name": props.get("title") or "",
        "type": nature,
        "procedure": ref.model_dump() if ref else None,
        "doc_id": doc_id,
        "project_id": project_id,
        "pinned": bool(props.get("pinned")),
        "edit_surface": surface,
        # Le point UNIQUE où l'adresse du tableau se résout : le jour où `title` cesse
        # d'être cette adresse, c'est cette ligne qui change, et les clients ne bougent pas.
        "datastore": (props.get("title") or None) if nature == "table" else None,
        "trail": [c.model_dump() for c in _fil(fiche, chaine)],
        "modified": NodeModified(
            at=str(fiche["updated_at"]) if fiche.get("updated_at") else None,
            by=_nom_de(props.get("created_by"))).model_dump(),
        # Ce qu'on ne sait pas encore servir, NOMMÉ. `modified.by` y figure quand le
        # nœud ne porte pas d'auteur : `props.created_by` est le CRÉATEUR, pas le
        # dernier éditeur — le servir comme tel serait une attribution fausse.
        "non_servi": ["access", "dependencies"]
        + ([] if props.get("created_by") else ["modified.by"]),
    }
    if corps["type"] == "table":
        # Le SCHÉMA, jamais les lignes. `child_schema` peut manquer : 29 des 83 tableaux
        # de production sont des tables libres (0054-D4) — `None` dit « libre », `[]`
        # dirait « aucune colonne », ce qui est faux.
        corps["columns"] = props.get("child_schema")
    else:
        corps["body"] = [_bloc(b) for b in db_node.blocks_of(fiche["id"])]
    # La surface annoncée doit arriver AVEC sa poignée : une fiche qui dirait `doc` sans
    # `doc_id` ferait deviner le client — exactement ce que le champ retire.
    try:
        exiger_poignee(surface, corps)
    except NoeudIncoherent as cause:
        raise _incoherent(fiche["public_id"], cause) from None
    corps["rev"] = _rev({k: v for k, v in corps.items() if k != "rev"})
    return corps


def _nom_de(sub: Optional[str]) -> Optional[str]:
    if not sub:
        return None
    return db_shell.names_of([sub]).get(sub)


async def _node(ctx: ResolvedCtx, inp: NodeInput):
    """Le nœud, ou un 304 si le client porte déjà notre version.

    Hors boucle, comme le rail : plusieurs requêtes DB, sur un serveur mono-loop.
    """
    corps = await run_in_threadpool(_compose, ctx, inp.node_id)
    if inp.rev and inp.rev == corps.get("rev"):
        return NotModified(corps["rev"])
    return corps


CAPABILITIES += [
    Capability(
        key="me.node", handler=_node, Input=NodeInput, Output=NodeOut,
        authz=ORG_MEMBER,
        description=(
            "Open ONE node by its opaque id: name, type, the TRAIL from the root "
            "(each crumb carrying its siblings, so a breadcrumb popover needs no "
            "extra call), and — for a page — its BODY as ordered blocks with STABLE "
            "ids you can cite. Opening a TABLE returns its column schema, never its "
            "rows (rows have their own cursor-paginated surface). Unknown id and "
            "forbidden id answer the SAME 404 on purpose: a 403 would reveal that the "
            "node exists. Pass `rev` for a conditional read (304 / `{not_modified}`). "
            "`non_servi` lists what this version cannot answer yet — read it before "
            "concluding that a node has no sharing or no dependants. `edit_surface` "
            "names the canonical surface that writes this node (node | doc | project | "
            "procedure | datastore | guide): it says WHERE to write, not WHETHER you "
            "may, and only `node` is written through `oto_node_edit`. PROVISIONAL "
            "surface: shape contracted, not frozen."),
        mcp="oto_node",
        rest=RestBinding("GET", "/api/me/nodes/{node_id}", provisoire=True),
    ),
]
