"""Le CONTRAT déclaré de `resources.govern` (`POST /api/resources`) : sa 200, ses refus.

Un front tiers, consommateur pur du REST, a signalé le 2026-09-01 que cette route
répondait `200 OK` **sans schéma** : le `requestBody` était décrit (dérivé de
l'`Input`), la réponse ne l'était par rien. On savait donc quoi envoyer, jamais ce
qui revenait — et « qui a accès » / « qui s'en sert » n'était pas dérivable.

**Pourquoi un module à part.** `resources.py` portait déjà 560 lignes pour un plafond
de 500 (`docs/conventions.md`) ; y écrire l'union et les onze refus l'aurait poussé
vers 620. Ce qui part ici est d'une seule nature — **ce que cette surface DÉCLARE**,
par opposition à ce qu'elle FAIT — donc la coupe suit un concept, pas la longueur.
Deux fichiers ne font pas un package : le module naît à plat, sans préfixe à créer.

**Pourquoi une UNION et pas une enveloppe commune.** `capability_output_debt.txt`
avait mesuré le 2026-08-11 que l'intersection des 7 `return` de cette surface est
**vide** : aucune clé n'est présente dans toutes les réponses. La conclusion d'alors
(« indescriptible en l'état ») portait sur l'enveloppe ; l'union complète des cinq
verbes, elle, se déclare sans cran technique — c'est ce que fait ce module.

**Le discriminant porte la distinction.** Les trois familles de ressource ne rendent
pas les mêmes clés (`row_count` pour un tableau, `archived_at` pour un projet,
`version` pour un guide). Une union PLATE qui les fondrait déclarerait `row_count`
sur un projet : une carte qui ment, pire qu'une carte absente, parce qu'un client
généré s'y branche. `resource_type` est donc un `Literal` par famille, et l'union est
discriminée dessus — le document rend `oneOf` + `discriminator`, ce qu'un générateur
de client sait traduire en type somme.

⚠️ **Ces modèles DÉCRIVENT, ils ne valident pas** (régime de `Capability.Output`,
cf. `_types.py`) : le handler continue de renvoyer un `dict`. Valider la sortie
ferait échouer un appel à l'exécution pour une divergence de contrat, ce qui punit
l'utilisateur d'un bug de serveur. La contrepartie est qu'une déclaration fausse ne
se voit pas à l'exécution — d'où les tests qui confrontent chaque modèle aux clés
réellement produites par les `_enrich_*` et par `projects._view()`.
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, RootModel

from .. import ownership
from . import _publication
from ._types import DeclaredError

# Les familles gouvernables. Source unique de l'énuméré publié ET du champ
# `resource_type` de l'entrée : `tests/test_resources_output.py` la confronte aux
# clés de `_OPS`, pour qu'un type ne puisse pas s'ajouter au dispatch sans entrer
# dans le contrat. `doc` = UNE page, partagée sans son projet (#1084).
#
# `procedure` s'est servie sous le mot d'avant #519 jusqu'au 23/09/2026
# (otomata-tech/oto#65) ; l'ancien nom est encore ACCEPTÉ en entrée, jusqu'à sa date
# (`deprecations.VALEURS`), et n'est plus jamais servi.
ResourceType = Literal["datastore_namespace", "project", "procedure", "doc", "app"]

# Famille PUBLIQUE → kind d'`ownership`, c.-à-d. la valeur ÉCRITE en base
# (`resource_grants.resource_type`). Elles coïncident partout sauf pour la procédure,
# dont la valeur stockée garde son nom d'avant #519 jusqu'à sa migration (lot D,
# #526). C'est la SEULE traduction entre les deux : la règle d'autz et le handler la
# lisent ici, donc aucun des deux ne peut adresser `ownership` sous le nom public.
KIND_OF: dict[str, str] = {
    "datastore_namespace": ownership.TYPE_RESSOURCE_DATASTORE,
    "project": "project",
    "procedure": ownership.TYPE_RESSOURCE_PROCEDURE,
    "doc": "doc",
    "app": "app",
}


class _Avertissement(BaseModel):
    """Ce que TOUTE réponse d'un appel servi par un alias daté porte en plus : l'avis,
    qui nomme la valeur d'aujourd'hui et la date de retrait (`deprecations.VALEURS`).
    Absent quand l'appel emploie déjà les noms d'aujourd'hui."""
    deprecation_warning: Optional[str] = None


class ResourceGrant(BaseModel):
    """Un bénéficiaire d'une ressource. `label` est ce que le front AFFICHE (email
    d'un user, nom d'une org/équipe) ; `principal_id` reste l'identifiant machine.
    `role` (ADR 0048) est la surface produit, `permission` la rétro-compat."""
    principal_type: Optional[str] = None
    principal_id: Optional[str] = None
    email: Optional[str] = None
    label: Optional[str] = None
    role: Optional[str] = None
    permission: Optional[str] = None
    granted_at: Optional[str] = None
    # #480 — sur un PROJET seulement : avec quelles clés ce bénéficiaire y travaille
    # (`own` = les siennes ; `inherit` = celles du propriétaire lui sont prêtées).
    credentials: Optional[Literal["own", "inherit"]] = None
    # otomata-tech/oto#39 — l'échéance du partage (null = sans échéance), et s'il est
    # ÉCHU : il reste listé, marqué, comme un jeton expiré — il ne donne plus rien.
    expires_at: Optional[str] = None
    expired: bool = False
    # Partage EN ATTENTE (29/09/2026) : l'adresse n'a pas encore de compte. Pas de
    # `principal_id` ; `invitation_expires_at` = l'échéance du lien envoyé.
    pending: bool = False
    invitation_expires_at: Optional[str] = None


class _OwnedResource(BaseModel):
    """Le socle commun aux trois familles : l'identité et le propriétaire.

    `owner_label` est résolu (nom d'org, nom d'équipe, email) — le front n'affiche
    jamais un `owner_id` brut. Ces trois clés sont le seul dénominateur commun ;
    tout le reste est propre à la famille, d'où les trois sous-classes.
    """
    resource_id: str
    owner_type: Optional[str] = None
    owner_id: Optional[str] = None
    owner_label: Optional[str] = None


class DatastoreResource(_OwnedResource):
    resource_type: Literal["datastore_namespace"]
    datastore: str
    row_count: int
    created_at: Optional[str] = None


class ProjectResource(_OwnedResource):
    resource_type: Literal["project"]
    name: str
    archived_at: Optional[str] = None
    created_at: Optional[str] = None


class GuideResource(_OwnedResource):
    """Une PROCÉDURE. `op=transfer` la DÉPLACE : même `resource_id`, toutes ses
    révisions, ses liens de projet et ses partages la suivent."""
    resource_type: Literal["procedure"]
    slug: str
    title: Optional[str] = None
    version: Optional[int] = None
    updated_at: Optional[str] = None


class DocResource(_OwnedResource):
    """UNE page. Son propriétaire est celui de son PROJET (une page n'en a pas
    d'autre) ; `title` la nomme, le corps n'est jamais servi ici."""
    resource_type: Literal["doc"]
    title: Optional[str] = None
    project_id: Optional[int] = None
    updated_at: Optional[str] = None


class AppResource(_OwnedResource):
    """UNE app (`oto_apps`). Son propriétaire et ses partages sont les siens ; les
    projets où elle est rangée ne donnent aucun accès."""
    resource_type: Literal["app"]
    title: Optional[str] = None
    updated_at: Optional[str] = None


GovernedResource = Annotated[
    Union[DatastoreResource, ProjectResource, GuideResource, DocResource, AppResource],
    Field(discriminator="resource_type"),
]


class ResourceList(_Avertissement):
    """`op=list` — ce que l'acteur gouverne dans UNE famille (un admin plateforme
    voit tout). Le `resource_type` de tête reprend celui demandé : la liste ne
    mélange jamais deux familles."""
    resource_type: ResourceType
    resources: list[GovernedResource]


# `op=get` = la fiche de la famille + ses bénéficiaires. L'héritage garde les trois
# formes et leur discriminant : dupliquer les champs ici les ferait diverger au
# premier ajout dans un `_enrich_*`.
class DatastoreResourceDetail(DatastoreResource, _Avertissement):
    grants: list[ResourceGrant]


class ProjectResourceDetail(ProjectResource, _Avertissement):
    grants: list[ResourceGrant]


class GuideResourceDetail(GuideResource, _Avertissement):
    grants: list[ResourceGrant]


class DocResourceDetail(DocResource, _Avertissement):
    grants: list[ResourceGrant]


class AppResourceDetail(AppResource, _Avertissement):
    grants: list[ResourceGrant]


ResourceDetail = Annotated[
    Union[DatastoreResourceDetail, ProjectResourceDetail, GuideResourceDetail,
          DocResourceDetail, AppResourceDetail],
    Field(discriminator="resource_type"),
]


class CascadeEntry(BaseModel):
    """Une entité liée touchée par la livraison d'un projet complet (#52).

    `_cascade_project` ne lève jamais : chaque entité rapporte son `status`. Les
    clés au-delà de `status` dépendent de l'issue (`role`/`permission` sur un
    partage, `new_ref`/`slug` sur une copie, `reason` sur un saut ou un échec) —
    toutes facultatives, parce qu'aucune n'est présente sur toutes les issues.
    """
    target_type: Optional[str] = None
    target_ref: Optional[str] = None
    label: Optional[str] = None
    status: Optional[Literal["shared", "transferred", "copied", "skipped",
                             "action_required", "failed"]] = None
    reason: Optional[str] = None
    role: Optional[str] = None
    permission: Optional[str] = None
    new_ref: Optional[str] = None
    slug: Optional[str] = None


class ResourceTransferred(_Avertissement):
    """`op=transfer`. `notified` n'est là que si le nouveau propriétaire est un
    user joignable par email — la notification est best-effort et ne casse jamais
    le transfert, donc son absence ne dit rien de l'échec du geste.

    Transférer une PROCÉDURE la DÉPLACE : `resource_id` inchangé, et ses révisions,
    ses liens de projet et ses partages la suivent. La cascade d'un PROJET, elle,
    COPIE ses procédures chez la cible (`cascade[].status == "copied"`, #52)."""
    ok: Literal[True]
    resource_id: str
    new_owner: Optional[str] = None
    cascade: Optional[list[CascadeEntry]] = None
    notified: Optional[bool] = None


class ResourceShared(_Avertissement):
    """`op=share` en audience `person`/`team`/`org` (ou sans audience — grant
    legacy). L'audience `public`/`secret`/`private` ne passe PAS par ici : elle
    publie le projet et rend `PublishedProject`."""
    ok: Literal[True]
    resource_id: str
    shared_with: Optional[str] = None
    principal_type: str
    role: str
    permission: str
    # #480 — rendu sur le partage d'un PROJET : les clés que ce partage déclare.
    credentials: Optional[Literal["own", "inherit"]] = None
    # otomata-tech/oto#39 — l'échéance ÉCRITE en base (null = sans échéance).
    expires_at: Optional[str] = None
    cascade: Optional[list[CascadeEntry]] = None
    notified: Optional[bool] = None
    # Partage EN ATTENTE vers une adresse sans compte (29/09/2026) : l'accès à CET
    # objet sera donné à l'inscription, jamais une adhésion. `pending_note` le dit
    # en clair ; `already_pending` = un partage identique attendait déjà (aucun
    # second lien) ; `invitation_expires_at` = l'échéance du lien.
    pending: Optional[bool] = None
    already_pending: Optional[bool] = None
    invitation_expires_at: Optional[str] = None
    pending_note: Optional[str] = None


class ResourceUnshared(_Avertissement):
    """`op=unshare`. `removed=False` = il n'y avait rien à révoquer (geste
    idempotent, pas un refus)."""
    ok: Literal[True]
    resource_id: str
    unshared_with: Optional[str] = None
    removed: bool
    cascade: Optional[list[CascadeEntry]] = None


class PublishedProject(BaseModel):
    """`op=share` en audience `public`/`secret` (publication MCP, ADR 0048 B3) ou
    `private` (dépublication) : le geste rend la **vue PROJET**, pas un accusé.

    ⚠️ Cette forme est celle de `projects._view()`, qui vit dans un autre module.
    La recopier ici est le seul moyen de la déclarer sans faire dépendre le
    chargement de `resources` de celui de `projects` (le cycle est réel : l'import
    y est paresseux, dans `_publish_audience`). Le prix de la copie est la dérive,
    et c'est un test qui le paie : `test_resources_output.py` confronte ces champs
    aux clés que `projects._view()` produit — un champ ajouté là-bas et pas ici
    fait rougir, plutôt que de faire mentir le document en silence.
    """
    id: int
    name: str
    icon: Optional[str] = None
    folder: Optional[str] = None
    position: Optional[float] = None
    url: Optional[str] = None
    brief_md: str
    owner_type: str
    owner_id: str
    # QUI voit ce projet, en clair (04/09/2026). `owner_type` seul oblige à dériver la
    # conséquence, et personne ne la dérive — surtout pas sur une question de
    # confidentialité, où l'on suppose le pire à raison. Recopié ici comme le reste de
    # la vue : c'est le prix du cycle d'import, et le test voisin le tient.
    visible_to: str
    context_org_id: Optional[str] = None
    is_template: bool
    mcp_slug: Optional[str] = None
    mcp_access: str
    mcp_tools: list[str]
    mcp_expose_datastore: bool
    mcp_expose_datastore_write: bool
    mcp_expose_docs: bool
    mcp_instructions_md: str
    excluded_url_prefixes: list[str]
    mcp_url: Optional[str] = None
    share_url: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    archived_at: Optional[str] = None
    # Ajouts conditionnels de `publish_project_mcp` — présents seulement quand il y
    # a quelque chose à dire (endpoint `org` enregistré chez Logto, outils non
    # résolvables, avertissements d'environnement de test).
    logto_resource_registered: Optional[bool] = None
    mcp_unresolvable_tools: Optional[list[str]] = None
    warnings: Optional[list[str]] = None


class ResourceOut(RootModel):
    """L'union COMPLÈTE des cinq verbes — ce que la 200 peut porter, et rien d'autre.

    L'ordre n'a pas de portée : `Output` décrit, il ne valide pas, donc aucune
    résolution d'ambiguïté n'a lieu à l'exécution. Ce qui compte est le `anyOf`
    publié et les DEUX `oneOf` discriminés du document : celui d'`op=get`, branche
    directe de l'`anyOf`, et celui des ENTRÉES d'`op=list`, un cran plus bas dans
    `ResourceList.resources.items`. Les deux comptent autant — c'est la liste qui
    déclarerait `row_count` sur un projet si on l'aplatissait.
    """
    root: Union[
        ResourceList,        # op=list
        ResourceDetail,      # op=get   (discriminé sur resource_type)
        ResourceTransferred,  # op=transfer
        ResourceShared,      # op=share  (audience person/team/org, ou legacy)
        PublishedProject,    # op=share  (audience public/secret/private)
        ResourceUnshared,    # op=unshare
    ]


# Les refus ATTEIGNABLES par les faces servies, et eux seuls (`Capability.errors`).
#
# ⚠️ **Deux codes levés par `resources.py` n'y sont volontairement PAS.** Les deux
# sont hors d'atteinte parce que la règle d'autz tourne AVANT le handler :
# `missing_resource_id` — `RESOURCE_GOVERN` lève déjà son propre `missing_resource` ;
# `not_found` — `can_govern` a refusé une ressource absente, il ne reste qu'une course
# entre l'autz et le handler. Déclarer un refus qu'on ne sait pas rejouer ferait
# promettre au document ce que le serveur ne rend pas, ce qui est pire qu'un document
# muet : un client généré s'y branche. Chaque entrée ci-dessous a son rejeu, et
# `test_resources_output.py` tient l'inventaire qui relie les deux.
REFUS: tuple[DeclaredError, ...] = (
    DeclaredError(400, "email_required",
                  "share/unshare sans principal : ni `email`, ni `org_id`, "
                  "ni `group_id`"),
    DeclaredError(400, "doc_viewer_only",
                  "`share` of a page (`resource_type=\"doc\"`) with a role other than "
                  "`viewer` — a page is shared read-only; share its project to let "
                  "someone write"),
    DeclaredError(400, "credentials_project_share_only",
                  "`credentials` sur autre chose que le partage d'un projet à une "
                  "personne, une équipe ou une org"),
    DeclaredError(400, "ttl_days_grant_only",
                  "`ttl_days` hors du partage d'une ressource à une personne, une équipe "
                  "ou une org (autre op, ou audience public/secret/private)"),
    DeclaredError(400, "publication_unsupported",
                  "audience `public`/`secret`/`private` sur autre chose qu'un "
                  "projet — seul un projet se publie"),
    DeclaredError(403, "forbidden",
                  "`transfer` demandé par un gérant : la cession de propriété est "
                  "réservée au propriétaire / à un admin"),
    # #1176 — audience public/secret par un gérant qui n'est pas membre de l'org
    # propriétaire : levé par `publish_project_mcp`, la règle unique de publication.
    _publication.REFUS_HORS_ORG,
    DeclaredError(403, "inherit_beyond_sharer_rights",
                  "`credentials='inherit'` demandé par qui n'est pas membre de l'org "
                  "propriétaire du projet — on ne prête que les clés qu'on atteint"),
    DeclaredError(403, "group_not_visible",
                  "grant d'équipe visant un groupe d'une org dont tu n'es pas membre"),
    DeclaredError(403, "not_group_member",
                  "`transfer` vers une équipe dont tu n'es ni membre ni admin"),
    DeclaredError(403, "group_outside_resource_org",
                  "`transfer` vers une équipe d'une autre org que la ressource — "
                  "une équipe ne fait pas changer d'org"),
    DeclaredError(403, "not_org_member",
                  "`transfer` vers une org dont tu n'es pas membre"),
    DeclaredError(404, "unknown_user",
                  "aucun compte : transfert vers une adresse inconnue, ou partage par "
                  "`sub` — un partage par `email` sans compte devient un partage EN "
                  "ATTENTE"),
    DeclaredError(400, "pending_share_plain_only",
                  "partage en attente (adresse sans compte) avec `cascade` ou "
                  "`credentials='inherit'` : à refaire une fois la personne inscrite"),
    DeclaredError(404, "unknown_org", "org destinataire inconnue"),
    DeclaredError(404, "unknown_group", "groupe destinataire inconnu"),
    DeclaredError(409, "confirm_loss_of_control",
                  "`transfer` qui te retirerait tout moyen de récupérer la "
                  "ressource — renvoyer avec `confirm_transfer=true`"),
    DeclaredError(409, "transfer_failed",
                  "la re-parentalisation a été refusée par le store"),
)

# Le refus que SEULE la surface héritée peut rendre. Son `resource_type` est un `str`
# libre — c'est ce qui lui permet de garder son défaut, donc d'être compatible — et la
# famille inconnue n'est refusée qu'au handler, par `_check_type`. Sur la surface
# stricte, le `Literal` la refuse une couche plus tôt, à la validation, et le code
# `invalid_input` sert alors le même office (mêmes égards que `op`).
#
# ⚠️ Il n'est donc PAS dans `REFUS` : ce que chaque surface DÉCLARE doit être ce
# qu'elle peut réellement rendre. Un refus déclaré et jamais rendu est pire qu'un refus
# tu — un client généré s'y branche.
REFUS_TYPE_INCONNU = DeclaredError(
    400, "unsupported_resource_type",
    "famille de ressource inconnue — surface héritée seulement, la stricte la refuse "
    "à la validation")
