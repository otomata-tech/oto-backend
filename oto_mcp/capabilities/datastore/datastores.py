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
- supprimer → la garde vit dans `store.delete_datastore` (`DatastoreForbidden`).

⚠️ **Le seul changement de comportement est voulu** : la validation de la couche
capacité REFUSE un champ inconnu (400 `unknown_fields`) là où ces routes l'ignoraient.
Conséquence à connaître : `oto data list --filter k:v` (oto-cli, via `oto-core`) envoie
un paramètre `filter` que cette route a cessé d'honorer il y a longtemps — il était
avalé en silence, il est maintenant nommé. Cf. `datastore/rows.py`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...datastore.identite import Adresse
from ...datastore.outils import adresse_servie

from ... import db, roles
from ...auth import token_scopes
from ...datastore.core import DatastoreExists, DatastoreForbidden, DatastoreNotFound, make_store
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .common import EntreeDatastore, HORODATAGE, govern_ns, ns_not_found
from ..registry import CAPABILITIES


# ⚠️ `url` était déclarée `str` — obligatoire et non nulle — alors que le store rend
# `None` pour tout compte dont le produit n'a pas de page de tableau (oto#63). Les
# sorties ne sont pas validées : le serveur servait donc `null` sous un contrat qui
# promettait une chaîne, et un client généré depuis ce contrat ne pouvait pas le prévoir.
URL_TABLEAU = ("Adresse de la page du tableau dans le produit de ce compte. `null` quand ce "
               "produit n'a pas de page de tableau — le tableau existe quand même ; "
               "`GET …/url` dit alors pourquoi (`url_absente`).")


class ListDatastoresInput(BaseModel):
    """Aucun paramètre : le périmètre est l'org active, jamais un argument."""


class CreateDatastoreInput(EntreeDatastore):
    # Défaut vide plutôt que champ requis : un nom manquant mérite le refus NOMMÉ
    # (`missing_datastore`) que cette route rend depuis toujours, pas l'`invalid_input`
    # générique de pydantic — le dashboard l'affiche tel quel.
    datastore: Adresse = ""
    # Classeur (ADR 0030) : `{type: 'org'|'group'|'user', id}`. Absent = PERSONNEL
    # (`type='user'`, l'appelant) — c'est ce que `_create_datastore` fait et ce que
    # `tests/datastore/test_datastore_datastores_capability.py` fige (`("create_datastore",
    # "vivier", "user", "u-1")`). Corrigé le 01/09/2026 (oto-backend#662) :
    # cette ligne annonçait « org active » depuis toujours, l'inverse du code servi,
    # et un tiers qui dérive son intégration du contrat crée alors chez lui un
    # tableau qu'il croit poser dans l'org. L'appartenance est vérifiée ici, jamais
    # présumée du corps.
    owner: Optional[dict] = None


class DatastoreRefInput(EntreeDatastore):
    datastore: Adresse


class RenameDatastoreInput(EntreeDatastore):
    datastore: Adresse
    name: str = ""


class DatastoreEntry(BaseModel):
    """Une entrée du catalogue de tableaux, telle que la peint le cockpit."""
    model_config = ConfigDict(populate_by_name=True)

    id: int
    # ⚠️ **Le MÊME nombre que `id`, sous le nom que tout le reste emploie** (07/09/2026).
    # `data_rows` et `data_get_schema` rendent le numéro du tableau sous `ns_id`, et la
    # description de `data_rows` est DIRECTIVE : « le NUMÉRO du tableau (`ns_id`) — la
    # forme à employer ». Qui la suit cherche donc `ns_id` dans une réponse qui n'avait
    # que `id`, et ne le trouve pas.
    #
    # Le coût mesuré n'est pas théorique : la confusion a produit ce soir une demande de
    # SUPPRESSION visant un tableau qui n'avait rien à voir avec la mission — une base
    # personnelle de plus de deux cents profils. Elle a été rattrapée parce qu'un humain
    # a lu le schéma avant de répondre, pas par une garde.
    #
    # ⚠️ **On AJOUTE, on ne renomme pas.** Le tableau de bord lit `id` et construit ses
    # liens dessus (`/data/<id>`) ; le renommer casserait un consommateur vivant pour
    # réparer un vocabulaire. Deux clés portant le même nombre est le prix de la
    # compatibilité — et c'est le sens de la lecture qui compte, pas l'économie d'octets.
    # DÉRIVÉ de `id` juste en dessous, jamais fourni par l'appelant : deux clés
    # stockées côte à côte finissent par diverger, une clé calculée ne le peut pas.
    ns_id: int = 0
    datastore: Adresse
    created_at: Optional[str] = Field(default=None, description=HORODATAGE)
    # Deep-link dashboard du tableau (`/data/<id>`) — dérivé de l'id, jamais stocké.
    url: Optional[str] = Field(default=None, description=URL_TABLEAU)
    # `True` = reçu par partage, `False` = possédé par l'org/l'équipe active.
    shared: bool
    owner_type: Optional[Literal["user", "org", "group"]] = Field(
        default=None, description=(
            "Qui possède ce tableau (ADR 0068). `user` : vous, privé par défaut — "
            "personne d'autre, pas même les admins de votre org, ne le voit. "
            "`group` : votre équipe. `org` : votre organisation entière. "
            "`null` : reçu par partage (`shared=true`), pas possédé."))
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

    @model_validator(mode="after")
    def _le_numero_sous_les_deux_noms(self):
        """`ns_id` recopie `id` — toujours, sans que la source ait à le savoir.

        Ni un défaut ni un doublon paresseux : c'est ce qui rend l'égalité VRAIE au
        lieu de la promettre. Un producteur qui poserait les deux pourrait en oublier
        un le jour où il change ; ici il n'y a qu'un nombre, servi sous deux noms."""
        if self.ns_id != self.id:
            object.__setattr__(self, "ns_id", self.id)
        return self


class DatastoreList(BaseModel):
    datastores: list[DatastoreEntry]


class CreatedDatastore(BaseModel):
    datastore: Adresse
    id: int
    url: Optional[str] = Field(default=None, description=URL_TABLEAU)
    # QUI possède le tableau — donc qui le verra. La création rendait moins que la
    # liste sur la seule information qui décide de ça (otomata-tech/oto#45) : le
    # serveur le savait, la réponse ne le disait pas.
    owner_type: Literal["user", "org", "group"] = Field(
        default="user", description=(
            "Qui possède ce tableau (ADR 0068). `user` : vous, privé par défaut. "
            "`group` : votre équipe. `org` : votre organisation entière."))
    owner_id: str = ""
    is_personal: bool = True
    # Posé QUAND le contexte d'org était là et que le tableau naît personnel quand
    # même. Un avertissement, jamais un refus : le défaut privé est voulu
    # (ADR 0068), c'est son silence qui coûtait une heure.
    avertissement: Optional[str] = None


class DeletedDatastore(BaseModel):
    ok: bool
    datastore: Adresse


class RenamedDatastore(BaseModel):
    ok: bool
    # Le NOUVEAU nom (l'id, l'URL et les partages, eux, ne bougent pas — ils sont
    # keyés par id).
    datastore: Adresse


class DatastoreUrl(BaseModel):
    url: Optional[str] = Field(default=None, description=URL_TABLEAU)
    url_absente: Optional[str] = Field(default=None, description=(
        "Présent SEULEMENT quand `url` est null : pourquoi ce tableau n'a pas d'adresse "
        "dans le produit de ce compte. Ce n'est jamais un tableau introuvable — celui-ci "
        "répond 404."))


def _list_datastores(ctx: ResolvedCtx, inp: ListDatastoresInput) -> dict:
    # Seule réponse FILTRÉE plutôt que refusée pour un jeton porté : sans le catalogue,
    # une intégration n'a pas le schéma de son tableau (`page_rows` ne le rend pas) —
    # elle ne pourrait pas peindre ses colonnes. No-op pour un JWT ou un jeton non porté.
    rows = token_scopes.filter_datastores(make_store(ctx.sub).list_datastores())
    return {"datastores": rows}


def _create_datastore(ctx: ResolvedCtx, inp: CreateDatastoreInput) -> dict:
    datastore = inp.datastore.strip()
    if not datastore:
        raise AuthzDenied(400, "missing_datastore")
    # ⚠️ Le défaut n'est PAS restitué ici, et c'est le point du lot du 08/09/2026 :
    # `owner_type=None` laisse le store appliquer `_default_owner` (ADR 0068). Le
    # restater ici (`or "user"`) faisait vivre le même défaut à deux endroits — et
    # surtout privait le store de la seule chose que la ROUTE sache : l'appelant
    # a-t-il NOMMÉ un propriétaire ? C'est ce fait-là qui décide de l'avertissement,
    # pas la valeur du propriétaire.
    owner = inp.owner or {}
    owner_type = ((owner.get("type") or "").strip() or None)
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
    elif owner_type is not None and owner_type != "user":
        raise AuthzDenied(400, "invalid_owner_type")
    try:
        # Le propriétaire, son identifiant, `is_personal` et l'`avertissement` sont
        # posés par le STORE et relayés tels quels — c'est ce qui interdit aux deux
        # faces de diverger (elles l'ont fait du 05 au 08/09 : cette réponse-ci les
        # portait, celle du tool MCP non, et sa description promettait le contraire).
        return make_store(ctx.sub).create_datastore(
            datastore, owner_type=owner_type, owner_id=owner_id)
    except DatastoreExists:
        raise AuthzDenied(409, "datastore_exists")


def _delete_datastore(ctx: ResolvedCtx, inp: DatastoreRefInput) -> dict:
    try:
        make_store(ctx.sub).delete_datastore(inp.datastore)
    except DatastoreNotFound:
        raise ns_not_found(ctx.sub, inp.datastore)
    except DatastoreForbidden:
        raise AuthzDenied(403, "forbidden")
    return {"ok": True, "datastore": inp.datastore}


def _rename_datastore(ctx: ResolvedCtx, inp: RenameDatastoreInput) -> dict:
    new = inp.name.strip()
    if not new:
        raise AuthzDenied(400, "name_required")
    ns_id = govern_ns(ctx.sub, inp.datastore)
    try:
        db.rename_datastore_by_id(ns_id, new)
    except ValueError as e:
        # Le message du store EST le code de refus ici (« datastore already exists »
        # côté db) : forme héritée de la route, conservée telle quelle — la changer
        # ferait mentir un front qui l'affiche.
        raise AuthzDenied(409, str(e))
    return {"ok": True, "datastore": new}


def _datastore_url(ctx: ResolvedCtx, inp: DatastoreRefInput) -> dict:
    try:
        return adresse_servie(make_store(ctx.sub).get_url(inp.datastore), ctx.sub)
    except DatastoreNotFound:
        raise ns_not_found(ctx.sub, inp.datastore)


_BASE = "/api/datastores"

CAPABILITIES += [
    Capability(
        key="me.datastore.list_datastores",
        handler=_list_datastores,
        Input=ListDatastoresInput,
        Output=DatastoreList,
        authz=SUB_ONLY,
        mcp=None,  # `data_list_datastores` tient déjà la face agent
        rest=RestBinding(verb="GET", path=_BASE),
        description="Liste les tableaux visibles dans l'org active (possédés et partagés).",
    ),
    Capability(
        key="me.datastore.create_datastore",
        handler=_create_datastore,
        Input=CreateDatastoreInput,
        Output=CreatedDatastore,
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
        key="me.datastore.delete_datastore",
        handler=_delete_datastore,
        Input=DatastoreRefInput,
        Output=DeletedDatastore,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="DELETE", path=_BASE + "/{datastore}"),
        description="Supprime un tableau, ses lignes et ses partages (droit de gouvernance).",
    ),
    Capability(
        key="me.datastore.rename_datastore",
        handler=_rename_datastore,
        Input=RenameDatastoreInput,
        Output=RenamedDatastore,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="PATCH", path=_BASE + "/{datastore}"),
        description="Renomme un tableau (id, URL et partages restent stables).",
    ),
    Capability(
        key="me.datastore.url",
        handler=_datastore_url,
        Input=DatastoreRefInput,
        Output=DatastoreUrl,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="GET", path=_BASE + "/{datastore}/url"),
        description="Deep-link dashboard d'un tableau.",
    ),
]
