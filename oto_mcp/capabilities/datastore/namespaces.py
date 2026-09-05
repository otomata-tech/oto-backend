"""Capacités « le tableau lui-même » : lister, créer, renommer, supprimer, ouvrir (#302).

Cinq chemins qui vivaient en routes écrites à la main (`api/datastore.py`) et
n'avaient donc **ni schéma d'entrée ni schéma de sortie** : un intégrateur qui génère
son client depuis `/api/openapi.json` n'en tirait rien, alors que le datastore est
l'écran central du produit. Mêmes chemins, mêmes réponses, mêmes refus — c'est une
migration de plomberie ; le dashboard ne doit rien voir.

`mcp=None` sur les cinq, opt-out explicite : les tools `data_*` existent déjà et ne
bougent pas (ce lot ne migre que la face REST). Le jour où une divergence apparaît
entre les deux faces, c'est une ligne `mcp=` ici, pas une seconde implémentation.

Autz `SUB_ONLY` au seuil ; le vrai gate reste **dans le handler**, où il était :
- lecture/écriture d'un tableau → résolue par le store (org active + ownership), un
  tableau hors périmètre répond 404 sans divulguer son existence ;
- renommer → `govern_ns`, c'est-à-dire `ownership.can_govern` (owner ∪ escalade
  `roles.py`, ADR 0030) — jamais un simple rôle d'org ;
- supprimer → la garde vit dans `store.delete_namespace` (`NamespaceForbidden`).

⚠️ **Le seul changement de comportement est voulu** : la validation de la couche
capacité REFUSE un champ inconnu (400 `unknown_fields`) là où ces routes l'ignoraient.
Conséquence à connaître : `oto data list --filter k:v` (oto-cli, via `oto-core`) envoie
un paramètre `filter` que cette route a cessé d'honorer il y a longtemps — il était
avalé en silence, il est maintenant nommé. Cf. `datastore/rows.py`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ... import db, roles
from ...auth import token_scopes
from ...datastore.core import NamespaceExists, NamespaceForbidden, NamespaceNotFound, make_store
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .common import HORODATAGE, govern_ns, ns_not_found
from ..registry import CAPABILITIES


class ListNamespacesInput(BaseModel):
    """Aucun paramètre : le périmètre est l'org active, jamais un argument."""


class CreateNamespaceInput(BaseModel):
    # Défaut vide plutôt que champ requis : un nom manquant mérite le refus NOMMÉ
    # (`missing_namespace`) que cette route rend depuis toujours, pas l'`invalid_input`
    # générique de pydantic — le dashboard l'affiche tel quel.
    namespace: str = ""
    # Classeur (ADR 0030) : `{type: 'org'|'group'|'user', id}`. Absent = PERSONNEL
    # (`type='user'`, l'appelant) — c'est ce que `_create_namespace` fait et ce que
    # `tests/datastore/test_datastore_namespaces_capability.py` fige (`("create_namespace",
    # "vivier", "user", "u-1")`). Corrigé le 01/09/2026 (oto-backend#662) :
    # cette ligne annonçait « org active » depuis toujours, l'inverse du code servi,
    # et un tiers qui dérive son intégration du contrat crée alors chez lui un
    # tableau qu'il croit poser dans l'org. L'appartenance est vérifiée ici, jamais
    # présumée du corps.
    owner: Optional[dict] = None


class NamespaceRefInput(BaseModel):
    namespace: str


class RenameNamespaceInput(BaseModel):
    namespace: str
    name: str = ""


class NamespaceEntry(BaseModel):
    """Une entrée du catalogue de tableaux, telle que la peint le cockpit."""
    model_config = ConfigDict(populate_by_name=True)

    id: int
    namespace: str
    created_at: Optional[str] = Field(default=None, description=HORODATAGE)
    # Deep-link dashboard du tableau (`/data/<id>`) — dérivé de l'id, jamais stocké.
    url: str
    # `True` = reçu par partage, `False` = possédé par l'org/l'équipe active.
    shared: bool
    owner_type: Optional[str] = None
    owner_id: Optional[str] = None
    # `read` | `write`. ⚠️ RABATTU sur la portée d'un jeton porté : un front qui peint
    # ses boutons dessus ne doit pas proposer une écriture que le serveur refusera.
    permission: Optional[str] = None
    can_write: bool
    # Gouvernance (renommer, partager, supprimer) — `ownership.can_govern`, pas un rôle.
    can_govern: bool
    is_personal: bool
    # Schéma typé déclaré (ADR 0046) ; `null` = table libre, l'état par défaut.
    # Le champ s'appelle `schema` sur le fil ; le nom python est décalé parce que
    # `schema` masque une méthode héritée de `BaseModel` (cf. `datastore/schema.py`).
    declared_schema: Optional[dict] = Field(default=None, alias="schema",
                                            serialization_alias="schema")


class NamespaceList(BaseModel):
    namespaces: list[NamespaceEntry]


class CreatedNamespace(BaseModel):
    namespace: str
    id: int
    url: str
    # QUI possède le tableau — donc qui le verra. La création rendait moins que la
    # liste sur la seule information qui décide de ça (otomata-tech/oto#45) : le
    # serveur le savait, la réponse ne le disait pas.
    owner_type: str = "user"
    owner_id: str = ""
    is_personal: bool = True
    # Posé QUAND le contexte d'org était là et que le tableau naît personnel quand
    # même. Un avertissement, jamais un refus : le défaut privé est voulu
    # (ADR 0068), c'est son silence qui coûtait une heure.
    avertissement: Optional[str] = None


class DeletedNamespace(BaseModel):
    ok: bool
    namespace: str


class RenamedNamespace(BaseModel):
    ok: bool
    # Le NOUVEAU nom (l'id, l'URL et les partages, eux, ne bougent pas — ils sont
    # keyés par id).
    namespace: str


class NamespaceUrl(BaseModel):
    url: str


def _list_namespaces(ctx: ResolvedCtx, inp: ListNamespacesInput) -> dict:
    # Seule réponse FILTRÉE plutôt que refusée pour un jeton porté : sans le catalogue,
    # une intégration n'a pas le schéma de son tableau (`page_rows` ne le rend pas) —
    # elle ne pourrait pas peindre ses colonnes. No-op pour un JWT ou un jeton non porté.
    rows = token_scopes.filter_namespaces(make_store(ctx.sub).list_namespaces())
    return {"namespaces": rows}


def _create_namespace(ctx: ResolvedCtx, inp: CreateNamespaceInput) -> dict:
    namespace = inp.namespace.strip()
    if not namespace:
        raise AuthzDenied(400, "missing_namespace")
    owner = inp.owner or {}
    owner_type = (owner.get("type") or "user").strip()
    owner_id = ctx.sub
    if owner_type == "org":
        try:
            org_id = int(owner.get("id"))
        except (TypeError, ValueError):
            raise AuthzDenied(400, "invalid_owner_id")
        if not roles.is_org_member(ctx.sub, org_id):
            raise AuthzDenied(403, "not_org_member")
        owner_id = str(org_id)
    elif owner_type == "group":
        try:
            group_id = int(owner.get("id"))
        except (TypeError, ValueError):
            raise AuthzDenied(400, "invalid_owner_id")
        if not roles.can_read_group(ctx.sub, group_id):
            raise AuthzDenied(403, "not_group_member")
        owner_id = str(group_id)
    elif owner_type != "user":
        raise AuthzDenied(400, "invalid_owner_type")
    try:
        cree = make_store(ctx.sub).create_namespace(
            namespace, owner_type=owner_type, owner_id=owner_id)
    except NamespaceExists:
        raise AuthzDenied(409, "namespace_exists")

    personnel = owner_type == "user"
    out = {**cree, "owner_type": owner_type, "owner_id": owner_id,
           "is_personal": personnel}
    # Le contexte d'org était posé (`X-Oto-Org` côté REST, `_org=` côté agent) et
    # le tableau naît quand même personnel : c'est juste — le propriétaire ne se
    # déduit JAMAIS du contexte (ADR 0068) — mais c'est le contraire de ce qu'on
    # attend d'un en-tête que toute la doc du datastore recommande pour « agir dans
    # l'org ». Sans cette phrase, tout marche sous cet en-tête et personne d'autre
    # ne voit le tableau : l'erreur ne se découvre qu'au second agent.
    # ⚠️ On regarde l'org EXPLICITEMENT demandée pour cet appel — la consultation
    # `X-Oto-Org` (REST) ou l'org du jeton `_org=` (agent) — jamais `ctx.org_id`.
    # Celui-ci vaut l'org ACTIVE, toujours posée puisqu'elle retombe sur la maison :
    # avertir dessus, ce serait avertir à CHAQUE création, y compris quand personne
    # n'a rien demandé de particulier. Un avertissement qui se déclenche toujours
    # ne se lit plus.
    from ... import session_org

    demandee = session_org.current_view_org() or session_org.current_call_org()
    if personnel and demandee and not (inp.owner or {}):
        out["avertissement"] = (
            f"Tableau créé PERSONNEL : toi seul le vois, même si l'organisation "
            f"{demandee} était le contexte de cet appel. Le contexte d'org ne "
            f"décide pas du propriétaire — seul `owner` le fait. Pour qu'il "
            f"appartienne à l'organisation : `owner: {{\"type\": \"org\", "
            f"\"id\": {demandee}}}` à la création (le propriétaire ne se change "
            f"pas après coup).")
    return out


def _delete_namespace(ctx: ResolvedCtx, inp: NamespaceRefInput) -> dict:
    try:
        make_store(ctx.sub).delete_namespace(inp.namespace)
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, inp.namespace)
    except NamespaceForbidden:
        raise AuthzDenied(403, "forbidden")
    return {"ok": True, "namespace": inp.namespace}


def _rename_namespace(ctx: ResolvedCtx, inp: RenameNamespaceInput) -> dict:
    new = inp.name.strip()
    if not new:
        raise AuthzDenied(400, "name_required")
    ns_id = govern_ns(ctx.sub, inp.namespace)
    try:
        db.rename_datastore_namespace_by_id(ns_id, new)
    except ValueError as e:
        # Le message du store EST le code de refus ici (« namespace already exists »
        # côté db) : forme héritée de la route, conservée telle quelle — la changer
        # ferait mentir un front qui l'affiche.
        raise AuthzDenied(409, str(e))
    return {"ok": True, "namespace": new}


def _namespace_url(ctx: ResolvedCtx, inp: NamespaceRefInput) -> dict:
    try:
        return {"url": make_store(ctx.sub).get_url(inp.namespace)}
    except NamespaceNotFound:
        raise ns_not_found(ctx.sub, inp.namespace)


_BASE = "/api/datastore/namespaces"

CAPABILITIES += [
    Capability(
        key="me.datastore.list_namespaces",
        handler=_list_namespaces,
        Input=ListNamespacesInput,
        Output=NamespaceList,
        authz=SUB_ONLY,
        mcp=None,  # `data_list_namespaces` tient déjà la face agent
        rest=RestBinding(verb="GET", path=_BASE),
        description="Liste les tableaux visibles dans l'org active (possédés et partagés).",
    ),
    Capability(
        key="me.datastore.create_namespace",
        handler=_create_namespace,
        Input=CreateNamespaceInput,
        Output=CreatedNamespace,
        authz=SUB_ONLY,
        mcp=None,
        # 201 : le code que cette route rend depuis toujours au dashboard et à oto-core.
        rest=RestBinding(verb="POST", path=_BASE, status=201),
        # ⚠️ 04/09/2026 : cette description annonçait « classeur d'org par défaut »
        # quand le code faisait `owner.get("type") or "user"`, donc PERSO — et la face
        # MCP du même verbe créait chez l'org en annonçant « per user ». Deux
        # propriétaires pour un seul geste, chaque texte affirmant le contraire de sa
        # propre face. L'ADR 0068 tranche l'écart dans le sens de CETTE face-ci : le
        # tableau naît personnel des deux côtés, et les deux textes disent la même
        # chose parce que les deux faces font la même chose.
        description=("Crée un tableau. Par défaut il est PERSONNEL (visible de toi "
                     "seul — ni les autres membres de ton org, ni ses administrateurs) ; "
                     "passe `owner: {type: \"org\"|\"group\", id: N}` pour qu'il "
                     "appartienne à l'org ou à l'équipe, et soit lisible de tous ses "
                     "membres. ⚠️ L'en-tête `X-Oto-Org` NE CHANGE PAS le propriétaire : "
                     "il décide sous quelle org on lit et écrit, jamais à qui appartient "
                     "ce qu'on crée — seul `owner` le fait, et il ne se change pas après "
                     "coup. Créé sous cet en-tête sans `owner`, le tableau naît personnel "
                     "et tout continue de fonctionner pour TOI : c'est au second agent, "
                     "ou au collègue qui ne le trouve pas, que ça se voit. La réponse "
                     "rend le propriétaire et vous avertit dans ce cas précis."),
    ),
    Capability(
        key="me.datastore.delete_namespace",
        handler=_delete_namespace,
        Input=NamespaceRefInput,
        Output=DeletedNamespace,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="DELETE", path=_BASE + "/{namespace}"),
        description="Supprime un tableau, ses lignes et ses partages (droit de gouvernance).",
    ),
    Capability(
        key="me.datastore.rename_namespace",
        handler=_rename_namespace,
        Input=RenameNamespaceInput,
        Output=RenamedNamespace,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="PATCH", path=_BASE + "/{namespace}"),
        description="Renomme un tableau (id, URL et partages restent stables).",
    ),
    Capability(
        key="me.datastore.url",
        handler=_namespace_url,
        Input=NamespaceRefInput,
        Output=NamespaceUrl,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="GET", path=_BASE + "/{namespace}/url"),
        description="Deep-link dashboard d'un tableau.",
    ),
]
