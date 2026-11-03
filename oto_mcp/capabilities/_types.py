"""Types de la couche capacité (ADR 0009).

Une `Capability` co-déclare, au même endroit que son handler : la clé stable,
le handler core, le modèle d'entrée pydantic (seule source de validation), une
règle d'autz **obligatoire**, et les bindings de surface (MCP / REST). Les
adaptateurs bouclent sur le registre et appliquent autz → validation → handler.

Aucun import d'adaptateur ni de transport ici (sens unique ADR 0004). Le refus
d'autz est un `AuthzDenied` **neutre** ; chaque adaptateur le traduit dans son
transport (McpError côté MCP, json_error+CORS côté REST).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Annotated, Callable, Optional

from pydantic import BaseModel, Field


@dataclass
class RawCtx:
    """Identité brute résolue par l'adaptateur (deux chemins d'auth distincts :
    ContextVar de token côté MCP, `authenticate(request)` côté REST)."""
    sub: Optional[str]


@dataclass
class ResolvedCtx:
    """Contexte enrichi produit par la règle d'autz, passé au handler.
    `org_id`/`group_id` sont injectés par la règle (jamais acceptés d'un param
    client → verrou IDOR par construction)."""
    # `None` = destinataire d'un projet publié sans login (ADR 0032, règle
    # `PROJECT_SHARED_READ`) — le handler doit alors se borner à la lecture du projet
    # publié. Toute autre règle exige un `sub` et ne produit jamais None.
    sub: Optional[str]
    org_id: Optional[int] = None
    role: Optional[str] = None
    group_id: Optional[int] = None
    # Vrai quand le principal est un worker de PLATEFORME authentifié par son
    # secret de machine (`WORKER_OR_ORG_MEMBER`) : pas de compte, pas d'org —
    # `org_id` est None et ce n'est pas un défaut. Le handler borne alors les
    # verbes à ceux du worker. Aucune autre règle ne le pose.
    platform_worker: bool = False
    # D'OÙ vient l'appel — `"mcp"` = un agent parle, `"rest"` = la face HTTP (le
    # dashboard, où un humain clique ; ou un intégrateur qui a écrit son code).
    #
    # Posé au SEUIL par les adaptateurs, jamais par une règle d'autz : les règles sont
    # communes aux deux faces, c'est le transport qui sait. Une règle qui le poserait
    # mentirait sur la moitié des appels.
    #
    # ⚠️ **Ce n'est pas un contrôle de sécurité, c'est un cran d'intention** (décision
    # d'Alexis, 04/09/2026 : « org = explicite, public = interdit à l'agent »). Il
    # empêche qu'une CONVERSATION rende un contenu lisible sans login — le chemin par
    # lequel un document marqué non diffusable s'est retrouvé partagé. Un agent qui
    # détient un jeton porté peut toujours appeler la face REST : le cran vise le geste
    # non voulu, pas l'adversaire. Dire l'inverse serait promettre une garantie qu'il
    # ne tient pas.
    #
    # ⚠️ `None` = ni l'un ni l'autre (appel interne, banc). Les gardes ne refusent que
    # sur `"mcp"` EXPLICITE, jamais sur « pas rest » : un adaptateur qui oublierait de
    # le poser rendrait alors la garde inerte — un vert qui ne prouve rien. C'est
    # pourquoi `tests/test_canal_d_appel.py` le lit sur le MONTAGE réel.
    channel: Optional[str] = None


class AuthzDenied(Exception):
    """Refus d'autz **neutre au transport**. `status` = code HTTP de référence
    (401/403/404/400) ; `code` = jeton machine stable ; `message` = détail.

    `details` = ce que le refus a de STRUCTURÉ, quand une phrase ne suffit pas au
    client pour agir. Optionnel et rare par construction : un refus se lit d'abord
    par son `code` et se comprend par son `message`. Le cas qui l'a fait naître
    (#487) est un tunnel de paiement à deux préalables — le client doit savoir
    QUELS documents, à QUELLE version et à QUELLE adresse, pour les afficher ; les
    recomposer en parsant une phrase française serait un contrat déguisé.

    Rendu par la face REST dans l'enveloppe d'erreur (`{"error", "detail",
    "details"}`), en plus du message, jamais à sa place. La face MCP ne rend que le
    message : son protocole n'a pas d'enveloppe d'erreur structurée, et les seules
    capacités qui posent `details` aujourd'hui sont REST-only (billing).

    `retry_after` = les secondes à attendre avant de réessayer, sur un refus qui passe
    (429 `sandbox_busy`) : la face REST le pose en en-tête `Retry-After`, la face MCP
    dans les données de l'erreur (`{code, retry_after_seconds}`)."""

    def __init__(self, status: int, code: str, message: str = "",
                 details: Optional[dict] = None, retry_after: Optional[int] = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.retry_after = retry_after


# Une règle d'autz : (identité brute, input validé) -> contexte résolu, ou lève AuthzDenied.
AuthzRule = Callable[[RawCtx, Optional[BaseModel]], ResolvedCtx]


class NotModified:
    """Sentinelle de retour : « rien n'a changé depuis la version que tu portes ».

    Un handler la renvoie au lieu d'un corps ; chaque adaptateur la traduit dans SON
    transport — REST une `304` sans corps (la seule forme qu'un cache HTTP comprend),
    MCP un `{not_modified: True, rev}` (le protocole n'a pas de code d'état, et une
    réponse vide s'y lirait comme un résultat vide).

    Pourquoi une sentinelle et pas un `dict` convenu : le handler ne connaît pas son
    transport (sens unique ADR 0004), et un `{"not_modified": true}` renvoyé en REST
    serait une **200 avec un corps** — le client rangerait « rien n'a changé » dans son
    cache à la place des données. La différence ne se voit pas en test d'unité ; elle se
    voit au deuxième appel d'un vrai client.
    """

    __slots__ = ("rev",)

    def __init__(self, rev: str):
        self.rev = rev


class EventStream:
    """Une réponse EN FLUX (`text/event-stream`) rendue par un handler : `events` est un
    itérable ASYNC d'octets, déjà au format SSE. L'adaptateur REST la sert telle quelle
    (CORS compris, `api/base._file_stream`) au lieu d'un JSON.

    ⚠️ Le corps s'écoule APRÈS la sortie du handler : les ContextVars que l'adaptateur
    pose autour de lui (geste, empreinte client, run) sont DÉJÀ retirées quand il tourne.
    Le flux pose lui-même ce dont il a besoin, et rend ses échecs DANS le flux — l'en-tête
    200 est parti. Un seul usage aujourd'hui : l'agent intégré (`capabilities/copilot`)."""

    def __init__(self, events):
        self.events = events


@dataclass(frozen=True)
class DeclaredError:
    """Un refus que la capacité DÉCLARE, pour que `/openapi.json` le porte.

    `Output` a mis un schéma sur la réponse heureuse (ADR 0059 §D7 : on ne fige que
    ce qui est généré). Les refus, eux, n'existaient que dans les `raise AuthzDenied`
    du handler : un intégrateur savait recevoir un 200 sans savoir quels 4xx attendre,
    et un front tiers a dérivé sa garde de saisie d'un contrat qui ne la disait pas
    (29/08/2026 — « 65 536 » n'était écrit nulle part de servi). Trois champs, tous
    publiés : le statut HTTP, le jeton `error` de l'enveloppe, et QUAND il survient.

    ⚠️ **DÉCRIT, ne fait rien.** Le handler continue de lever `AuthzDenied(status,
    code)` ; la déclaration n'ajoute ni garde ni traduction. Elle n'est donc vraie
    que si le code est bien levé — `tests/test_capability_declared_errors.py` l'exige
    dans le module du handler, et chaque déclaration a son test qui REJOUE le refus
    sur la route servie. Une déclaration sans rejeu est décorative, ce qui est pire
    qu'une absence : un document qui promet un 409 que le serveur ne rend pas.

    La liste d'une capacité n'est pas exhaustive par construction (les 400 de
    l'adaptateur — `invalid_input`, `unknown_fields`, `invalid_json`, `invalid_body` —
    valent pour toutes) ; le document le dit dans son préambule.
    """
    status: int
    code: str
    when: str                                   # quand il survient — phrase publiée


@dataclass(frozen=True)
class ContratDeRoute:
    """Le contrat d'une route de NATURE — hors du moule capacité par construction (corps
    binaire, pas de JWT) — que `/openapi.json` publie au lieu de la souche « écrite à la
    main » (oto#106).

    Même matière que la capacité, et rien d'autre : une description, la forme de la
    réponse heureuse (`Output`), les refus DÉCLARÉS (`DeclaredError`, chacun ATTEIGNABLE
    depuis le handler — `tests/test_capability_declared_errors.py`), plus ce que seule
    une telle route a : le corps par verbe (type de média → schéma) et l'absence
    d'`Authorization`. Posé sur le handler (`handler.contrat = …`) : le document le lit
    sur la table de routes vivante, là où il trouve déjà le chemin et les méthodes.
    """
    description: str
    Output: Optional[type[BaseModel]] = None
    errors: tuple = ()
    # verbe → {type de média: schéma} ; un verbe absent n'a pas de corps.
    corps: dict = field(default_factory=dict)
    # Type de média de la réponse heureuse (une page d'upload rend du HTML).
    media: str = "application/json"
    # False = aucun en-tête d'auth : le jeton de l'URL fait foi.
    authentifiee: bool = True


@dataclass(frozen=True)
class RestBinding:
    verb: str                                   # GET | POST | PUT | PATCH | DELETE
    path: str                                   # ex "/api/me/active-org"
    # placeholder de route -> champ Input, quand ils diffèrent (routes réelles en {id}).
    path_map: dict = field(default_factory=dict)
    # Code de la réponse heureuse. 200 partout, SAUF là où un chemin historique rend
    # déjà 201 (création d'un tableau, ajout d'une ligne) : ce code est servi au
    # dashboard et à `oto-core` depuis toujours, le ramener à 200 en migrant la route
    # serait une régression silencieuse — la migration ne doit rien changer au fil.
    status: int = 200
    # Le corps JSON **entier** EST la valeur de CE champ d'`Input`, au lieu d'être
    # fusionné clé par clé dans les données validées.
    #
    # Pour les chemins dont le corps est une DONNÉE libre : la ligne d'un tableau,
    # dont les colonnes appartiennent à l'utilisateur — aucune ne peut être déclarée
    # dans un modèle. Sans ce cran, la garde de champ inconnu refuserait chacune
    # d'elles : elle vise un client qui se trompe de FORME, pas un client qui envoie
    # ses propres données. Déclaré par binding (donc greppable), jamais deviné.
    body_field: Optional[str] = None
    # Lire le corps JSON même sur un verbe qui n'en porte pas d'ordinaire (DELETE).
    # Cas historique : `DELETE …/namespaces/{ns}/share {"email": …}`, dont le client
    # vit hors de ce dépôt (`oto-core`) ; et depuis #523 la révocation d'un jeton
    # (`DELETE …/tokens/{id} {"reason": …}`). Opt-in explicite : le défaut reste
    # « pas de corps sur un DELETE », sinon migrer une route pourrait faire apparaître
    # un 400 `unknown_fields` sur un corps jusque-là ignoré.
    reads_body: bool = False
    # Route réservée à une SESSION INTERACTIVE : un porteur de jeton API `oto_` y est
    # refusé (403 `api_token_forbidden`).
    #
    # Deux usages, et ce sont des GARDES, pas des préférences : la gestion des jetons
    # eux-mêmes — un jeton qui peut en créer d'autres rend sa fuite auto-entretenue
    # (révoquer le jeton fuité ne suffit plus, l'attaquant s'en est fait un second, non
    # expirant), émettre un jeton reste donc un acte humain ; et l'agent intégré à
    # l'application (`capabilities/copilot`) — un jeton en ferait un point d'accès au
    # modèle payé par l'instance, piloté par un script.
    #
    # Le cran vit sur le BINDING et non sur la capacité parce qu'il est propre au
    # transport : côté MCP, la question ne se pose pas (l'appelant est déjà une session).
    # Sans lui, migrer ces six routes aurait été une régression de sécurité — c'est pour
    # ça qu'elles étaient restées écrites à la main.
    allow_api_token: bool = True
    # Sur une route `allow_api_token=False`, laisse passer le jeton ÉMETTEUR (portée
    # `issue`) et lui seul. Posé sur les trois routes de MES jetons, jamais sur celles
    # du palier admin.
    allow_issuer_token: bool = False
    # Surface DÉCLARÉE PROVISOIRE : forme attendue, pas contrat figé. Publié tel quel
    # dans l'OpenAPI (`x-oto-provisoire: true`), la convention que le front a proposée
    # et qu'on a prise. Dire « provisoire » DANS le document est ce qui autorise à
    # servir tôt : un consommateur qui s'y branche sait qu'il s'y branche à ses frais,
    # et personne n'a à déduire d'une absence de mention que la forme est gravée.
    provisoire: bool = False
    # Type de média de la réponse heureuse. `text/event-stream` pour une route dont le
    # handler rend un `EventStream` : le document le publie tel quel.
    media: str = "application/json"


@dataclass(frozen=True)
class Capability:
    key: str                                    # clé stable, ≠ nom de surface (ex "org.use_org")
    handler: Callable                           # (ResolvedCtx, Input) -> dict (logique core)
    Input: type[BaseModel]                       # seule source de validation
    authz: AuthzRule                            # OBLIGATOIRE (pas de défaut → oubli = TypeError)
    # Forme de la RÉPONSE. L'entrée est dérivable depuis `Input` (schéma OpenAPI,
    # schéma de tool MCP) ; la sortie ne l'était par rien — elle n'existait que dans
    # les `return` du handler. Un consommateur tiers savait donc APPELER sans savoir
    # ce qu'il recevrait, et devait sonder. C'est la contrainte d'ADR 0059 prise au
    # mot : **on ne fige que ce qui est généré** — donc rien n'était figeable côté
    # réponse. `Output` déclaré ⟹ `/openapi.json` porte le schéma de la 200.
    # `None` = non déclarée : toléré pour l'existant, INTERDIT pour une capacité
    # neuve (garde-fou `tests/test_capability_outputs.py`, dette qui ne peut que
    # décroître). Le handler continue de renvoyer un `dict` — `Output` DÉCRIT, il
    # ne valide pas : valider la sortie ferait échouer un appel à l'exécution pour
    # une divergence de contrat, ce qui punit l'utilisateur d'un bug de serveur.
    Output: Optional[type[BaseModel]] = None
    # Les REFUS que la capacité publie (statut, jeton `error`, quand) — cf.
    # `DeclaredError` : décrit, ne fait rien, et chaque entrée a son test de rejeu.
    errors: tuple = ()
    description: str = ""                       # contrat LLM du tool MCP
    mcp: Optional[str] = None                   # nom du tool MCP, ou None (opt-out explicite)
    # un OU plusieurs bindings REST (ex. routes self-service + admin sur le même
    # métier+autz), ou None (opt-out explicite).
    rest: "Optional[RestBinding | tuple[RestBinding, ...]]" = None
    # Cette capacité change le PROFIL de visibilité (org/groupe actif) : après le
    # handler, l'adaptateur MCP re-pousse la denylist de la nouvelle org sur la
    # session courante → `tools/list_changed` live (B2/B3). No-op côté REST (le
    # dashboard n'est pas une session MCP).
    refresh_visibility: bool = False
    # Feature flag optionnel (dark launch) : callable 0-arg évalué au MONTAGE
    # (make_routes REST / register MCP), pas à l'import → le descripteur reste
    # dans le registre (introspection, tests, catalogue) mais sa surface n'est
    # pas exposée si le gate rend faux. Piloté par env par-déploiement (prod off,
    # canari on) sans divergence de branche. None = toujours exposé.
    gate: "Optional[Callable[[], bool]]" = None

    def __post_init__(self):
        if self.mcp is None and not self.rest:
            raise ValueError(
                f"Capability {self.key!r} sans surface : déclarer mcp= et/ou rest= "
                f"(un opt-out doit être explicite, pas un oubli)."
            )

    def is_exposed(self) -> bool:
        """False = déclarée mais NON montée (feature flag off à ce déploiement)."""
        return self.gate is None or bool(self.gate())

    def rest_bindings(self) -> list[RestBinding]:
        if self.rest is None:
            return []
        if isinstance(self.rest, RestBinding):
            return [self.rest]
        return list(self.rest)


def cap_limit(value, maximum: int, *, default: Optional[int] = None) -> int:
    """Borne une taille de page — **écrête, ne refuse pas**.

    Le patron vient de la recherche (`SearchInput._cap`) et vaut pour toute lentille
    paginée : sans lui, une valeur énorme part telle quelle au SQL et une valeur
    négative fait échouer la requête en 500 (oto-backend#300). Écrêter plutôt que
    refuser est un choix : le client qui demande trop reçoit le maximum servable, et
    non une erreur qu'il devra apprendre à éviter.

    `default` sert aux consoles op-aware, dont le `limit` est `Optional` parce qu'il
    dépend du verbe (un export ne se pagine pas comme une timeline).
    """
    if value is None:
        value = default if default is not None else maximum
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default if default is not None else maximum
    return max(1, min(value, maximum))


def apply_flat_signature(fn: Callable, model: type[BaseModel]) -> Callable:
    """Expose les champs de `model` en paramètres KEYWORD_ONLY plats sur `fn`.

    FastMCP (3.4.2) génère le schéma d'un tool depuis la signature : un unique
    param pydantic donnerait un schéma IMBRIQUÉ (`{"p": {"$ref": …}}`), cassant
    le contrat plat des tools existants. On injecte donc `__signature__` +
    `__annotations__` reconstruits depuis les champs du modèle → schéma plat.
    Validé empiriquement (ADR 0009 §6 ; test `test_with_signature_flat`).

    **La `description` d'un champ voyage, et elle seule** (#627). Recopier annotation
    et défaut ne suffisait pas : un `Field(description=…)` posé sur un `Input` était
    ACCEPTÉ-INERTE — la face REST le publiait (`Input.model_json_schema()`), la face
    MCP le perdait, schéma servi 621 caractères avant, 621 après. Les outils portés
    par une capacité ne pouvaient donc pas suivre `docs/conventions.md` « ce qu'un
    préambule d'outil AUTORISE se répète dans la description du PARAMÈTRE concerné »
    (#517), quand les outils écrits à la main le pouvaient. *Le texte le plus proche
    du geste est celui que l'agent relit à chaque appel : une permission qui n'atteint
    pas le schéma servi est une consigne écrite dans le vide.*

    **Les contraintes de `f.metadata` voyagent aussi** (08/09/2026, retours
    796-798). En pydantic v2, ce
    qu'un `Annotated[…]` porte — `BeforeValidator`, `Ge`/`Le`, `pattern`,
    `WithJsonSchema` — ne vit PAS dans `f.annotation` (qui rend le type nu) mais dans
    `f.metadata`. Les laisser derrière rendait toute coercition ou borne déclarée sur
    un `Input` **acceptée-inerte côté MCP** : elle mordait sur la face REST, qui
    valide par l'`Input` lui-même, et se taisait sur la face agent. Mesuré en
    production le 08/09/2026 — `data_write` (tool écrit à la main) acceptait le
    numéro de tableau en entier, `data_get_schema` / `data_patch_schema` /
    `data_drop_column` (portés par une capacité) le refusaient. *Deux outils du même
    module au contrat divergent : chercher la cause dans le métier fait perdre du
    temps, le partage est ici entre un tool et une capacité.*

    ⚠️ **`examples` et `json_schema_extra`, eux, ne voyagent toujours pas.**
    `tools/list` part à tout client MCP connecté, alors que le document REST est un
    contrat d'intégration : ce qu'un champ porte pour le second n'a pas à sortir par
    le premier (#582). Ces deux-là vivent sur le `FieldInfo`, pas dans `f.metadata` —
    c'est pourquoi on continue de construire un `Field` NEUF avec la seule
    description au lieu de faire suivre le `FieldInfo` du modèle : faire suivre
    l'objet emporterait ce qu'on vient d'exclure, et l'emporterait encore quand un
    champ gagnera un attribut qu'on n'a pas prévu.

    Le cliquet est `tests/test_contraintes_servies_face_agent.py` : il parcourt le
    REGISTRE, pas une liste d'outils, et prouve d'abord sur le montage réel qu'une
    coercition déclarée mord bien à l'appel.
    """
    params = []
    annotations: dict = {}
    for name, f in model.model_fields.items():
        default = inspect.Parameter.empty if f.is_required() else f.default
        # L'ordre est celui de la déclaration : les contraintes du champ d'abord
        # (telles qu'`Annotated[…]` les portait), la description ajoutée après.
        portees = list(f.metadata)
        if f.description:
            portees.append(Field(description=f.description))
        annotation = Annotated[(f.annotation, *portees)] if portees else f.annotation
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                                        annotation=annotation, default=default))
        annotations[name] = annotation
    fn.__signature__ = inspect.Signature(params)
    fn.__annotations__ = annotations
    return fn
