"""Écrire un nœud — la face d'écriture du nouvel univers de contenu.

Le nouvel univers **ne projette pas** : ce qu'on écrit ici naît dans `nodes` et n'a
aucune source dans l'ancien monde, comme les couches de contexte depuis M1. Les deux
surfaces vivent côte à côte le temps de la transition, et l'ancienne continue de
servir l'ancien monde sans rien savoir de celle-ci (arbitrage d'Alexis, 31/08/2026 :
« on ne migre pas, on arrête la recopie, la surface nœud vit à côté et part de vide »).

⚠️ **Trois genres, et rien d'autre** : `page`, `tableau`, `ligne`. Projet, guide et
procédure ne sont PAS des genres — ce sont des rôles portés en propriété par une page
(ADR 0054-D5 : « le genre dit ce que l'objet EST, et ce qu'il JOUE est un rôle porté
en propriété, jamais un `kind` de plus »). Cette surface n'en introduira pas.

⚠️ **Aucun nœud écrit ici ne porte `delivery`.** La recherche discrimine une couche de
contexte par cette propriété, pas par le genre : la poser sur une page ordinaire la
ferait remonter dans le périmètre des couches, celui qu'on injecte au handshake.

Le palier d'écriture reste celui des guides (`guides._owner_for_write` — plateforme /
org / chef d'équipe / soi) : en écrire une seconde version la ferait diverger de la
première au premier changement.

⚠️ **Mais ce palier RÉSOUT une identité, il n'AUTORISE pas** : au cran personne il rend
`ctx.sub` sans regarder la cible qu'on lui passe. La garde, ici, est de comparer ce
qu'il rend au propriétaire réel du nœud (`_proprietaire`) — l'appeler et jeter sa
réponse, c'était n'avoir aucune garde du tout. Corrigé le 2026-09-01 après une mesure
de bout en bout en production : un porteur quelconque écrivait et supprimait le nœud
privé d'autrui, là où la lecture du même nœud lui rendait 404.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..db import node_tables as db_node_tables, nodes as db_nodes, node_view as db_node
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .node_keys import NoeudIncoherent, edit_surface_de, refus_guide
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)


class NodeEditInput(BaseModel):
    op: Literal["create", "update", "move", "delete"]
    # create
    scope: Optional[str] = None          # platform | org | group | user (défaut : user)
    owner_id: Optional[str] = None       # cible explicite du scope
    # Le GENRE, et il n'y en a que trois (0054-D5). Une ligne se crée sous son
    # tableau : `kind="ligne"` + `parent_id` du tableau + `data`.
    kind: Optional[Literal["page", "tableau", "ligne"]] = None
    title: Optional[str] = None
    description: Optional[str] = None
    body_md: Optional[str] = None
    # tableau : le schéma de ses enfants. Facultatif — une table libre est un
    # tableau valide (29 des 83 tableaux de production n'en déclaraient aucun).
    columns: Optional[list[dict]] = None
    # ligne : les valeurs métier. Elles vont dans la colonne `data`, JAMAIS dans
    # `props` — une cellule nommée `title` ne doit pas écraser le titre du nœud.
    data: Optional[dict] = None
    # update / move / delete
    node_id: Optional[str] = None        # l'id opaque, jamais l'id interne
    parent_id: Optional[str] = None      # move : le nouveau parent (id opaque), ou null
    after_id: Optional[str] = None       # move : le frère après lequel se placer


class NodeEditOut(BaseModel):
    ok: bool
    id: Optional[str] = None
    op: str


def _introuvable() -> AuthzDenied:
    """LE refus de cette surface — un seul objet, pour l'inconnu comme pour l'interdit.

    Un 403 sur l'un des deux ferait du code d'état un oracle d'existence ; deux
    MESSAGES différents le referaient du corps de la réponse. Les deux refus sont donc
    le même, au caractère près — et c'est exactement ce que la face de lecture rend
    déjà, pour que « pas lisible » et « pas écrivable » ne se distinguent pas non plus.
    """
    return AuthzDenied(404, "not_found",
                       "Aucun nœud de ce nom, ou aucun droit de le voir.")


def _interne(node_id: str) -> dict:
    """La ligne d'un nœud depuis son id PUBLIC, ou le même 404 qu'à la lecture."""
    fiche = db_node.node_by_public_id(node_id)
    if not fiche:
        raise _introuvable()
    return fiche


def _proprietaire(ctx: ResolvedCtx, fiche: dict) -> None:
    """Être propriétaire de CE nœud — et c'est la COMPARAISON qui le vérifie.

    ⚠️ **`_owner_for_write` résout une identité, il n'autorise pas.** Il rend le
    propriétaire POUR LEQUEL l'appelant a le droit d'écrire à ce palier ; au palier
    personne il fait `return ctx.sub` — il dérive l'identité de l'appelant **sans
    jamais regarder l'identifiant qu'on lui passe**. Son commentaire (« jamais
    d'écriture pour un AUTRE user ») est vrai chez lui, où l'on écrit son propre mode
    d'emploi et où aucune cible tierce n'existe. Réemployé ici comme vérificateur, il
    ne gardait rien : sa valeur de retour était jetée, donc aucune comparaison n'avait
    lieu, et n'importe quel porteur authentifié écrivait et supprimait le nœud privé
    d'autrui — mesuré de bout en bout en production le 2026-09-01 (v1.172.0), avec un
    404 à la lecture du même nœud.

    La garde est donc la ligne `accorde != proprietaire` ci-dessous, et elle vaut pour
    TOUS les paliers, y compris celui qu'on ajoutera demain : le jour où un scope
    résout une identité sans la vérifier, l'écart se voit ici au lieu de passer.

    ⚠️ **`props.legacy` n'a jamais protégé cet étage.** Tant que la recopie tournait,
    tout nœud `user` était une copie et le 409 tombait avant — l'étage ne s'exécutait
    jamais. Son arrêt (2026-09-01) l'a ouvert sans qu'un test bouge, et le retrait des
    copies rendrait le défaut général. C'est un accident de calendrier, pas une garde.
    """
    proprietaire = str(fiche["owner_id"])
    from .guides import _owner_for_write
    try:
        accorde = _owner_for_write(ctx, str(fiche["owner_type"]), proprietaire)
    except AuthzDenied:
        # Tout refus du palier ressort en 404 indistinct : « réservé à un admin de
        # l'org » dirait à un étranger que le nœud existe, et sous quel toit.
        raise _introuvable() from None
    if str(accorde) != proprietaire:
        raise _introuvable()


def _s_ecrit_ici(fiche: dict) -> None:
    """Ce nœud s'écrit-il ICI ? C'est la surface que sa fiche annonce qui en décide :
    `node_keys.edit_surface_de`, le MÊME prédicat que lit `oto_node` (oto#198). Une
    annonce et un refus écrits séparément finiraient par se contredire, et le client
    croirait la fiche.

    - un nœud CONVERTI (`doc`, `project`, `procedure`, `datastore`) appartient à l'ancien
      monde : sa source y est la vérité, l'écrire des deux côtés ferait diverger les
      deux (`409 node_projete`) ;
    - une couche de contexte (`guide`) s'écrit par `oto_guide`, et le refus nomme l'appel
      exact (`409 node_guide`). ⚠️ Ce passage rendait 200 jusqu'au 13/09/2026 : il
      contournait la borne de 65 536 octets que `oto_guide` pose sur un corps — le texte
      injecté au handshake avait deux écrivains, dont un sans borne. Son usage réel n'a
      pas été mesuré ;
    - un stockage qui ne sait pas dire sa surface lève `noeud_incoherent`, plutôt que de
      passer pour natif (un `legacy` vide passait l'ancien test de vérité).

    Se juge APRÈS la propriété : un tiers qui sonde un identifiant ne doit pas apprendre
    qu'il désigne une copie ou un guide — ce serait le même oracle par une autre porte.
    """
    props = fiche.get("props") or {}
    try:
        surface = edit_surface_de(props)
    except NoeudIncoherent as cause:
        logger.error("nœud %s incohérent : %s", fiche["public_id"], cause)
        raise AuthzDenied(500, "noeud_incoherent",
                          f"Le nœud {fiche['public_id']} ne peut pas dire où il "
                          f"s'écrit : {cause}.") from None
    if surface == "node":
        return
    if surface == "guide":
        message, details = refus_guide(fiche["owner_type"], fiche["owner_id"], props)
        raise AuthzDenied(409, "node_guide", message, details=details)
    raise AuthzDenied(
        409, "node_projete",
        "Ce nœud est une copie de l'ancien monde : il s'édite sur sa surface "
        "d'origine. Seuls les nœuds nés ici s'écrivent ici.")


def _mien(ctx: ResolvedCtx, fiche: dict) -> None:
    """Le palier complet pour ÉCRIRE ce nœud : en être propriétaire, et qu'il s'écrive
    ici (ni copie, ni couche de contexte). Ce qu'on ne fait que TOUCHER (le parent d'une
    création, l'ancre de rang d'un déplacement) n'en demande que la moitié —
    `_proprietaire` — parce que « s'écrire ici » parle de ce qu'on édite, pas de ce
    qu'on vise."""
    _proprietaire(ctx, fiche)
    _s_ecrit_ici(fiche)


def _create(ctx: ResolvedCtx, inp: NodeEditInput) -> dict:
    """Les trois genres passent par le MÊME verbe, et c'est le modèle qui le veut.

    Un tableau et une ligne ne sont pas des objets à part : ce sont des nœuds, avec
    un genre et une place dans l'arbre. Leur donner chacun son verbe créerait trois
    vocabulaires pour une seule notion — et trois endroits où l'autorisation, la
    place dans la fratrie et le refus d'écrire une copie devraient rester d'accord.
    """
    from .guides import _owner_for_write
    genre = (inp.kind or "page").strip()
    # ⚠️ **On ne range pas chez quelqu'un d'autre** — la règle que `move` tenait déjà
    # et que la création ignorait. Sans elle, greffer sa page sous le nœud privé d'un
    # tiers réussissait (200), le `trail` servi au greffon rendait le TITRE de ce
    # parent — une fuite de lecture par le rail, sur un nœud que la face de lecture
    # refuse d'ouvrir — et la suppression du parent par son propriétaire emportait le
    # greffon, `delete_page` ramassant la descendance sans regarder à qui elle est.
    parent_fiche = _interne(inp.parent_id) if inp.parent_id else None
    if parent_fiche is not None:
        _proprietaire(ctx, parent_fiche)
    parent = parent_fiche["id"] if parent_fiche is not None else None

    if genre == "ligne":
        # Une ligne n'a PAS de propriétaire propre (0054-D4) : elle prend celui de
        # son tableau. Le scope demandé n'a donc pas de sens ici — c'est le palier
        # d'écriture SUR LE TABLEAU qui décide, et rien d'autre.
        if parent_fiche is None:
            raise AuthzDenied(400, "missing_parent_id",
                              "Une ligne se crée sous son tableau : `parent_id` requis.")
        # Une ligne EST le contenu de son tableau : l'y écrire, c'est écrire le
        # tableau — d'où le palier complet, copie et guide compris, et non la seule
        # propriété.
        _s_ecrit_ici(parent_fiche)
        row = db_node_tables.add_row(parent, inp.data or {})
        if row is None:
            raise AuthzDenied(400, "parent_pas_un_tableau",
                              "`parent_id` doit désigner un tableau né ici.")
        return {"ok": True, "id": row["public_id"], "op": "create"}

    # ADR 0068 : le défaut était `org` — un nœud créé sans préciser naissait lisible de
    # tous les membres. Le palier exigeait déjà `org_admin` (`_owner_for_write`), donc
    # personne n'élargissait à son insu sans en avoir le droit ; mais « en avoir le
    # droit » n'est pas « l'avoir voulu », et c'est un agent qui appelle.
    scope = (inp.scope or "user").strip()
    owner_id = _owner_for_write(ctx, scope, inp.owner_id)
    if not (inp.title or "").strip():
        raise AuthzDenied(400, "missing_title",
                          f"`title` requis pour créer un nœud de genre {genre}.")
    if genre == "tableau":
        row = db_node_tables.create_table(owner_type=scope, owner_id=owner_id,
                                          title=inp.title, columns=inp.columns,
                                          parent_id=parent)
        return {"ok": True, "id": row["public_id"], "op": "create"}

    row = db_nodes.create_page(owner_type=scope, owner_id=owner_id,
                               title=inp.title, body_md=inp.body_md or "",
                               description=inp.description or "", parent_id=parent)
    return {"ok": True, "id": row["public_id"], "op": "create"}


def _update(ctx: ResolvedCtx, inp: NodeEditInput) -> dict:
    """Ce qu'on peut écrire dépend du GENRE — c'est le nœud qui le dit, pas l'appel.

    On ne demande pas à l'appelant de redéclarer le genre : il est déjà stocké, et
    le lui faire répéter ouvrirait un écart entre ce qu'il croit modifier et ce
    qu'il modifie vraiment.
    """
    fiche = _interne(_besoin(inp.node_id, "update"))
    _mien(ctx, fiche)
    genre = fiche.get("kind")

    if genre == "ligne":
        if not db_node_tables.update_row(fiche["id"], inp.data or {}):
            raise AuthzDenied(400, "rien_a_ecrire",
                              "Aucune cellule fournie — `data` attendu pour une ligne.")
        return {"ok": True, "id": fiche["public_id"], "op": "update"}

    # Un titre ABSENT ne change rien et n'est pas exigé ; un titre FOURNI vide ou blanc
    # est refusé AVANT toute écriture (colonnes comprises), et stocké taillé comme à la
    # création (oto#198). Sans ce refus, un `title` vide effaçait le nom du nœud et un
    # blanc le remplaçait par des espaces : `update_page` pose tout ce qui n'est pas None.
    titre = None
    if inp.title is not None:
        titre = inp.title.strip()
        if not titre:
            raise AuthzDenied(400, "missing_title",
                              "`title` fourni vide : un nœud garde un titre. Omets "
                              "`title` pour ne pas le changer.")

    fait = False
    if genre == "tableau" and inp.columns is not None:
        fait = db_node_tables.set_columns(fiche["id"], inp.columns)
    # Le titre et la description valent pour une page comme pour un tableau ; le
    # corps n'a de sens que pour une page, et `update_page` l'ignore s'il est absent.
    fait = db_nodes.update_page(fiche["id"], title=titre,
                                description=inp.description,
                                body_md=inp.body_md) or fait
    if not fait:
        raise AuthzDenied(400, "rien_a_ecrire",
                          "Aucun champ fourni — `title`, `description`, `body_md` "
                          "(page) ou `columns` (tableau).")
    return {"ok": True, "id": fiche["public_id"], "op": "update"}


def _move(ctx: ResolvedCtx, inp: NodeEditInput) -> dict:
    fiche = _interne(_besoin(inp.node_id, "move"))
    _mien(ctx, fiche)
    parent = None
    if inp.parent_id:
        cible = _interne(inp.parent_id)
        _mien(ctx, cible)                 # on ne range pas chez quelqu'un d'autre
        parent = cible["id"]
    after = None
    if inp.after_id:
        # ⚠️ L'ancre de rang n'était gardée par RIEN, et c'est une fuite à elle seule :
        # `_interne` rend 404 sur un id inconnu et la fiche sur un id existant, donc
        # deux sondes confirmaient l'existence d'un nœud d'autrui. Les couches de
        # contexte ont un identifiant DÉRIVÉ (`db/guides._public_id_sql`), donc
        # devinable depuis le seul `sub` de la victime : l'oracle était exploitable
        # sans jamais avoir vu un identifiant. Une ancre légitime est de toute façon
        # un FRÈRE — même propriétaire que le nœud déplacé, `place_after` bornant la
        # fratrie à ce propriétaire — donc cette garde ne refuse rien de réel.
        ancre = _interne(inp.after_id)
        _proprietaire(ctx, ancre)
        after = ancre["id"]
    db_nodes.move_page(fiche["id"], parent_id=parent, after_id=after)
    return {"ok": True, "id": fiche["public_id"], "op": "move"}


def _delete(ctx: ResolvedCtx, inp: NodeEditInput) -> dict:
    fiche = _interne(_besoin(inp.node_id, "delete"))
    _mien(ctx, fiche)
    db_nodes.delete_page(fiche["id"])
    return {"ok": True, "id": fiche["public_id"], "op": "delete"}


def _besoin(node_id: Optional[str], op: str) -> str:
    if not (node_id or "").strip():
        raise AuthzDenied(400, "missing_node_id", f"`node_id` requis pour {op}.")
    return node_id


_OPS = {"create": _create, "update": _update, "move": _move, "delete": _delete}


async def edit(ctx: ResolvedCtx, inp: NodeEditInput) -> dict:
    """Hors boucle : plusieurs requêtes DB par appel, sur un serveur mono-loop."""
    return await run_in_threadpool(_OPS[inp.op], ctx, inp)


CAPABILITIES += [
    Capability(
        key="me.node.edit", handler=edit, Input=NodeEditInput, Output=NodeEditOut,
        # Plancher : être membre de l'org active. Le PALIER réel (plateforme, org,
        # équipe, soi) est tranché dans le handler par `guides._owner_for_write` —
        # le même modèle que les guides, jamais une seconde version.
        authz=ORG_MEMBER,
        description=(
            "Write a node in the NEW content universe (op=create | update | move | "
            "delete). THREE kinds, and only three: `page`, `tableau`, `ligne` "
            "(default page). A table row is a node too: create it with "
            "kind='ligne', `parent_id` of its table, and `data` holding the cell "
            "values — `data` is where user values live, never `props`, so a column "
            "named `title` cannot overwrite the node's own title. A table's column "
            "schema is `columns`, optional (a free table is a valid table), and "
            "re-posting it REPLACES it. Nodes written here are NATIVE: they have no "
            "source in the old world, nothing refreshes them, and the old surfaces "
            "(`oto_doc`, `oto_project`) do not see them — the two universes live "
            "side by side during the transition. A page BODY is stored as ordered "
            "blocks with STABLE ids, so editing a title never re-identifies the "
            "paragraphs and citations survive. `scope` picks the owner (platform | "
            "org | group | user, default user — the node is PRIVATE, yours alone: "
            "name a scope to make it your org's or your team's) and follows the SAME "
            "write ladder as "
            "guides; a row has no owner of its own, it takes its table's. `move` "
            "changes parent and rank WITHOUT changing identity — that is what makes "
            "children, blocks and inbound references survive. You may only write, "
            "move or delete a node you OWN, and only file one under a parent you own: "
            "anything else answers the SAME 404 as reading it, unknown and forbidden "
            "being indistinguishable. Editing a node that is a COPY of the old world "
            "is refused (409): it is edited on its own surface. So is a context layer "
            "(a guide, 409), the refusal naming the `oto_guide` call that writes it. "
            "On update, an empty or blank `title` is refused (400): omit `title` to "
            "keep it. PROVISIONAL surface, like its read side."),
        mcp="oto_node_edit",
        rest=RestBinding("POST", "/api/me/nodes/edit", provisoire=True),
    ),
]
