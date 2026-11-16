"""Descriptif OpenAPI de l'API REST — **dérivé**, jamais tenu à la main.

Sans descriptif, toute intégration se construit par sondage : on tape des chemins
plausibles et on lit les codes de retour. C'est ainsi qu'un intégrateur a conclu que
les projets n'étaient pas sur REST — il avait sondé `/api/projects` (404) sans jamais
essayer `POST /api/me/projects {"op":"list"}`, qui existe. La surface n'était pas
absente : elle était indescriptible.

Deux sources, aucune saisie :

1. **Le registre de capacités** (`capabilities/registry.py`) — chaque capacité porte
   déjà son chemin, son verbe, sa description et le **JSON Schema** de son `Input`
   pydantic. C'est la même matière que sert `/api/admin/capabilities`.
2. **La table de routes vivante** de l'application Starlette, pour les routes encore
   écrites à la main (datastore, OAuth, jetons…). On n'a que chemin + méthodes : elles
   sont documentées comme telles, sans schéma, plutôt que passées sous silence.

⚠️ Conséquence de la consolidation ADR 0047 : le verbe d'un objet métier vit dans le
corps (`op`), pas dans le chemin. `POST /api/me/projects` n'est pas « créer un
projet » — c'est **la** surface projet (`op` ∈ create|list|get|runs|…), et l'énuméré
`op` du schéma le dit. Le document rend donc lisible ce que les chemins cachent.

Le document est **public** (comme `/api/mcp/catalog`) : il décrit des formes, aucune
valeur. `/api/admin/*` en est retiré — la console de la plateforme n'a pas
d'intégrateur tiers, et sa carte n'a pas à être publiée.

**Deux documents, une seule recette** (oto-backend#1196) : `build(admin=True)` rend le
document d'ADMINISTRATION, les opérations `/api/admin/*` et elles seules, TOUTES (aucune
liste à tenir : une capacité neuve sous ce préfixe y entre d'elle-même). Il est servi
derrière la garde d'admin plateforme (`GET /api/admin/openapi.json`, capacité
`admin.openapi`), et c'est lui que la garde d'avant déploiement confronte au contrat
admin qu'un consommateur épingle. Les deux documents se partagent le préfixe, jamais un
chemin : le public n'a pas changé d'un octet.
"""
from __future__ import annotations

from typing import Iterable, Optional

from . import deprecations, version
from .capabilities import registry
from .capabilities._types import Capability, ContratDeRoute, RestBinding

_TITLE = "Oto REST API"
_TITLE_ADMIN = "Oto REST API — administration"
_BODY_VERBS = ("POST", "PUT", "PATCH")
_ADMIN_PREFIX = "/api/admin/"
# Les routes de NATURE qui déclarent leur contrat : ni capacités (aucun en-tête de run,
# aucun refus de l'adaptateur), ni souches « legacy ».
_TAG_NATURE = "_nature"

# Le MODE D'EMPLOI du document — la seule prose qu'un intégrateur lit avant son premier
# appel, et elle est ICI, dans ce qu'on sert. Elle a vécu un temps dans le build de
# docs.oto.cx, collée devant cette description : tout consommateur qui ne passait pas par
# cette page (un générateur de client, un agent qui lit le contrat, un intégrateur qui
# reçoit le fichier) ne la voyait jamais (oto-dashboard#155). Ce qui gouverne un appel se
# DÉCLARE en plus dans le contrat (en-têtes `X-Oto-Org`/`X-Oto-Group`, `_parametres`) :
# la prose explique, elle ne remplace pas une déclaration.
#
# ⚠️ Aucune adresse d'instance en dur : le même document est servi par chaque instance.
# Les exemples notent `$OTO` l'adresse du serveur, que `servers` porte.
_DESCRIPTION = """\
API REST de la plateforme Oto. Deux faces servent le même métier (ADR 0009) : le
MCP (`/mcp`, JWT Logto) et ce REST. Ce document est **dérivé du serveur** : construit
une fois par processus, au premier appel, puis servi depuis la mémoire avec un `ETag`
— il décrit ce qui tourne, pas une intention.

## Démarrer

Trois choses à savoir avant le premier appel : où obtenir un jeton, ce que ce jeton
ouvre, et dans quelle organisation il travaille. Les exemples notent `$OTO` l'adresse
de ce serveur (le champ `servers` de ce document).

### 1. Obtenir un jeton

Chaque appel porte `Authorization: Bearer <jeton>`, sous deux formes :

- **JWT Logto** — la session interactive (dashboard, connecteur MCP) ;
- **jeton API** `oto_…` — l'intégration programmatique.

Un jeton API se crée depuis une **session interactive** : l'écran **Développeurs** du
compte, dans la console (`/account/developers`). Un jeton ne peut ni en lister, ni en
créer, ni en révoquer un autre : c'est volontaire, il ne peut pas se réémettre lui-même
s'il fuite. Il n'est affiché **qu'une fois**, à sa création.

### 2. Le premier appel

```bash
curl -s "$OTO/api/me/profile" \\
  -H "Authorization: Bearer oto_…"
```

Une réponse `200` prouve que le jeton est valide. Un `401 {"error":"invalid_api_token"}`
qu'il ne l'est pas ou plus ; un `401 {"error":"missing_bearer"}` que l'en-tête manque.

Si ton jeton est **porté** (section suivante), cet appel répond `403` : `/api/me/profile`
n'est pas dans sa portée. Vérifie-le alors sur un tableau qu'il nomme, par exemple
`GET /api/datastores/<ton-tableau>/rows`.

### 3. Ce qu'un jeton ouvre

Par défaut, **un jeton est le compte** : le porteur peut ce que la personne peut. C'est
la forme à garder pour ses propres scripts, jamais celle à confier à un tiers.

Pour une intégration tierce, crée un jeton **porté** (`POST /api/me/tokens` avec
`scopes`) — la posture s'inverse, rien n'est permis sauf ce que la portée nomme :

```json
{
  "label": "front client",
  "scopes": {
    "namespaces": { "204": "read", "317": "write" },
    "projects": { "12": "read" }
  }
}
```

Un tableau se nomme par son **identifiant** (`ns_id`) : un renommage ne déplace pas ce que
le jeton atteint. `read` lit le tableau, `write` lit **et** écrit ses lignes. Ni l'un ni l'autre n'ouvre la
gouvernance — créer, renommer, supprimer ou partager un tableau reste hors de portée, comme
tout le reste de l'organisation. Hors portée, la réponse est `403 token_scope_forbidden`.

Un tableau est nommé par son **nom**, un projet par son **id**. Un renommage déplace donc
ce que le jeton atteint : ré-émets-le après.

### 4. Choisir l'organisation

Sans rien préciser, un appel travaille dans ton **organisation maison** (`home_org` de
`GET /api/me`). Pour en viser une autre, ajoute l'en-tête `X-Oto-Org` (et `X-Oto-Group`
pour une équipe) ; tes organisations se listent avec `GET /api/me/orgs`. Les deux
en-têtes sont **déclarés sur chaque opération** qui les lit (composants `XOtoOrg` et
`XOtoGroup`) — un client généré les connaît.

```bash
curl -s "$OTO/api/me/search?q=facture" \\
  -H "Authorization: Bearer oto_…" \\
  -H "X-Oto-Org: 42"
```

⚠️ Un compte membre de **plusieurs** organisations qui omet l'en-tête ne reçoit aucune
erreur : ses appels lisent et écrivent dans l'org maison. C'est juste tant qu'il n'en a
qu'une ; dès la deuxième, ce qu'il cherche est ailleurs. L'en-tête décide aussi sous quelle
org on lit et écrit, **jamais à qui appartient ce qu'on crée** — seul `owner` le fait.

### 5. Le verbe est parfois dans le corps

Les surfaces consolidées — projets, pages, procédures, ressources — exposent **un seul
chemin** dont le corps porte l'action :

```bash
curl -s "$OTO/api/me/projects" \\
  -H "Authorization: Bearer oto_…" \\
  -H "Content-Type: application/json" \\
  -d '{"op": "list"}'
```

Les valeurs possibles sont dans l'énuméré `op` du schéma de la requête, sur chaque
opération concernée.

### 6. Déposer en volume

Pour charger des milliers de lignes d'un coup, pas ligne à ligne : `POST
/api/me/upload-url` frappe un lien signé à usage unique (mêmes paramètres que l'outil
agent `oto_upload_url`), puis le contenu part en `PUT` brut sur ce lien, **sans**
`Authorization` — NDJSON ou CSV, upserté en lot sur la clé du tableau. Corps, accusé et
refus : `PUT /api/upload/{token}`. Un jeton porté ne frappe pas de lien.

## Erreurs

Une seule enveloppe, le composant `Erreur` : `error` (jeton machine stable, la clé sur
laquelle un client décide), `detail` (la phrase, actionnable) et parfois `details` (forme
structurée). Les 4xx listés sur une opération sont ceux qu'elle DÉCLARE, chacun rejoué par
un test ; la liste n'est pas exhaustive : tout appel peut rendre `400 invalid_input` /
`unknown_fields` / `invalid_json` / `invalid_body` (validation de la requête par
l'adaptateur), `401` et `403`.

## Ce que ce document ne dit pas encore

Il est **dérivé du serveur**, pas rédigé : il décrit ce qui tourne. Mais il hérite aussi
de ses trous, et les montrer vaut mieux que les taire.

Une partie des opérations n'a pas encore de **schéma de réponse** déclaré — leur réponse
heureuse n'a pas de `content`, la forme du `200` n'est pas décrite. Et les opérations
taguées `_legacy` sont écrites à la main plutôt que dérivées du registre de capacités :
ni leur corps ni leur réponse ne peuvent être décrits tant qu'elles restent dehors. Ces
deux dettes se résorbent côté serveur, et ce document suivra sans intervention. Les
opérations taguées `_nature` sont hors du moule par construction (corps brut, jeton dans
l'adresse) mais DÉCLARENT leur contrat : corps, réponse et refus y sont décrits.
"""

# Le document d'ADMINISTRATION (#1196) ne répète pas le mode d'emploi : son lecteur est un
# consommateur de la console (qui épingle ce qu'il appelle), pas un intégrateur qui débute.
_DESCRIPTION_ADMIN = """\
Les opérations d'administration de la plateforme (`/api/admin/*`), toutes, dérivées du
serveur comme le document public — qui ne les contient pas. Réservé aux administrateurs
de la plateforme. Jeton, organisation, enveloppe d'erreur : voir le document public
(`/api/openapi.json`), dont ce document reprend les composants.
"""


def _du_document(path: str, admin: bool) -> bool:
    """Un chemin appartient à UN document : l'administration (`/api/admin/*`) ou le
    public (tout le reste). Le seul aiguillage entre les deux — un second, ailleurs,
    finirait par publier une route d'admin dans le document servi sans authentification."""
    return path.startswith(_ADMIN_PREFIX) == admin


# L'enveloppe d'erreur REST (`api/base._json_error`), publiée UNE fois en composant :
# un client généré la type une seule fois, et un `$ref` cassé ferait échouer la
# génération entière — d'où un composant toujours présent, jamais conditionnel.
_ERREUR = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {"type": "string",
                  "description": "jeton machine stable — la clé sur laquelle décider"},
        "detail": {"type": "string",
                   "description": "la phrase, actionnable ; jamais à parser"},
        "details": {"type": "object",
                    "description": "forme structurée du refus, quand une phrase ne "
                                   "suffit pas au client pour agir (rare)"},
    },
}
_ERREUR_REF = {"$ref": "#/components/schemas/Erreur"}

# L'en-tête de RUN d'une requête (oto#227) — UN composant, référencé par les opérations de
# CAPACITÉS et par elles seules : l'adaptateur des capacités est le seul à le lire. Une
# route écrite à la main ou un alias 308 ne peut ni le lire ni rendre ses refus, et le
# leur déclarer dirait faux (un refus se déclare là où il peut survenir, #217).
_PARAM_RUN = "XOtoRun"
_PARAM_RUN_REF = {"$ref": f"#/components/parameters/{_PARAM_RUN}"}

# Les en-têtes de CONTEXTE (oto-dashboard#155) — l'organisation, ou l'équipe, dans
# laquelle l'appel lit et écrit. Ils décident de la portée d'une écriture : c'est un
# paramètre, pas une note de prose. Absents du contrat, un client généré n'avait aucun
# moyen de savoir qu'ils existent, et ses appels atterrissaient dans l'org maison — ce
# qui MARCHE, jusqu'au jour où le compte en a deux.
#
# Qui les lit : `api.routes.ViewAsMiddleware`, sur TOUTE requête `/api/*` authentifiée,
# avant la route — capacité ou route écrite à la main. D'où leur référence sur ces deux
# familles, et pas sur un alias 308 (qui ne fait que renvoyer ailleurs : c'est la cible
# qui les lit). Même source pour les deux faces : l'en-tête pose l'org de consultation
# que résout le seam `access.current_org`.
_PARAM_ORG = "XOtoOrg"
_PARAM_GROUP = "XOtoGroup"
_PARAMS_CONTEXTE = ({"$ref": f"#/components/parameters/{_PARAM_ORG}"},
                    {"$ref": f"#/components/parameters/{_PARAM_GROUP}"})


def _parametres() -> dict:
    return {
        _PARAM_RUN: {
            "name": "X-Oto-Run", "in": "header", "required": False,
            "schema": {"type": "string"},
            "description": (
                "Le run de la requête (`POST /api/me/runs`) — le seul titulaire qu'un bail "
                "de ligne reconnaisse. Jugé AVANT l'opération, refus nommés sans rien "
                "écrire : run inconnu ou d'un autre compte, porteur non membre de l'org du "
                "run (403 générique), `X-Oto-Org` contradictoire, run clos.")},
        _PARAM_ORG: {
            "name": "X-Oto-Org", "in": "header", "required": False,
            "schema": {"type": "string", "examples": ["42", "perso"]},
            "description": (
                "L'organisation dans laquelle l'appel lit et écrit : son id (`GET "
                "/api/me/orgs`), ou `0` / `perso` pour l'espace personnel. **Absent : "
                "l'organisation maison du compte** (`home_org` de `GET /api/me`) — sans "
                "erreur, donc un compte de plusieurs orgs qui l'oublie écrit dans sa maison. "
                "Une org dont le porteur n'est pas membre → `403 forbidden`. Il choisit OÙ "
                "on lit et écrit, jamais à qui appartient ce qu'on crée (`owner`). Une valeur "
                "illisible est ignorée : l'appel reste dans l'org maison.")},
        _PARAM_GROUP: {
            "name": "X-Oto-Group", "in": "header", "required": False,
            "schema": {"type": "integer", "minimum": 1},
            "description": (
                "L'équipe dans laquelle l'appel travaille, par son id ; son organisation "
                "parente devient celle de l'appel (elle l'emporte sur `X-Oto-Org`). "
                "Absent : le niveau de l'organisation. Une équipe que le porteur ne peut "
                "pas lire → `403 forbidden` ; une valeur illisible est ignorée.")},
    }


def _refus_de_l_en_tete() -> tuple:
    """Les refus que `X-Oto-Run` peut rendre sur TOUTE opération de capacité, déclarés
    une fois à côté du contrôle qui les lève. Import paresseux : le module des runs
    DÉCLARE ses capacités à l'import, et l'ordre fixe celui de la table de routes."""
    from .capabilities.run_thread import REFUS_DECLARES_DE_L_EN_TETE
    return REFUS_DECLARES_DE_L_EN_TETE


def _reponse_erreur(description: str, codes: Optional[list[str]] = None) -> dict:
    """Une réponse d'erreur : l'enveloppe, et l'énuméré des `error` possibles quand
    on les connaît — c'est ce qu'un client généré sait lire et sur quoi il branche."""
    schema: dict = _ERREUR_REF if not codes else {
        "allOf": [_ERREUR_REF, {"properties": {"error": {"enum": codes}}}]}
    return {"description": description,
            "content": {"application/json": {"schema": schema}}}


def _reponses(cap: Capability, heureuse: tuple[str, dict]) -> dict:
    """Les réponses d'une opération : l'heureuse, les deux refus génériques, puis les
    refus DÉCLARÉS (`Capability.errors`) regroupés par statut — deux codes sur un même
    409 font UNE réponse dont l'énuméré porte les deux. Un refus déclaré sur un statut
    générique (403) s'AJOUTE à la description, il ne remplace pas le générique : le
    `forbidden` de l'autz reste possible.

    ⚠️ Les refus de l'en-tête `X-Oto-Run` (oto#227) entrent dans la MÊME fusion, sur toute
    capacité : le contrôle se joue avant chaque opération de l'adaptateur. Un code déjà
    déclaré par la capacité n'est pas doublé."""
    out = {heureuse[0]: heureuse[1],
           "401": _reponse_erreur("jeton absent ou invalide"),
           "403": _reponse_erreur("refus d'autorisation (ou hors portée du jeton)")}
    return _fusionne_refus(out, (*cap.errors, *_refus_de_l_en_tete()))


def _fusionne_refus(out: dict, errors: Iterable) -> dict:
    """Range des refus déclarés dans les réponses `out`, par statut (cf. `_reponses`).
    Partagé par les capacités et les routes de nature qui déclarent leur contrat."""
    par_statut: dict[int, list] = {}
    vus: set = set()
    for e in errors:
        if (e.status, e.code) in vus:
            continue
        vus.add((e.status, e.code))
        par_statut.setdefault(e.status, []).append(e)
    for statut, errs in sorted(par_statut.items()):
        phrase = " ; ".join(f"`{e.code}` — {e.when}" for e in errs)
        if str(statut) in out:
            out[str(statut)] = _reponse_erreur(out[str(statut)]["description"] + " ; " + phrase)
        else:
            out[str(statut)] = _reponse_erreur(phrase, [e.code for e in errs])
    return out


def _param(name: str, location: str, schema: dict, required: bool,
           description: str = "") -> dict:
    out = {"name": name, "in": location, "required": required,
           "schema": schema or {"type": "string"}}
    if description:
        out["description"] = description
    return out


def _placeholders(path: str) -> list[str]:
    """Noms de placeholders d'un chemin Starlette, `{id}` ou `{id:int}`."""
    out, rest = [], path
    while "{" in rest:
        _, _, rest = rest.partition("{")
        name, _, rest = rest.partition("}")
        out.append(name.split(":")[0])
    return out


def _openapi_path(path: str) -> str:
    """`/x/{id:int}` → `/x/{id}` (le convertisseur Starlette n'est pas de l'OpenAPI)."""
    for ph in _placeholders(path):
        for raw in (f"{{{ph}:int}}", f"{{{ph}:path}}", f"{{{ph}:str}}", f"{{{ph}:float}}"):
            path = path.replace(raw, f"{{{ph}}}")
    return path



def _section(key: str) -> str:
    """La section de la doc REST pour une capacité — sa RESSOURCE, pas sa portée
    d'autorisation (oto-backend#330).

    Le tag valait `key.split(".")[0]`, c'est-à-dire `me`, `org`, `admin` : le préfixe
    dit QUI a le droit, pas SUR QUOI. Conséquence pour qui découvre l'API : les routes
    d'un même objet étaient éparpillées entre deux sections selon qu'on les atteint pour
    soi (`me.datastore.*`) ou pour son org (`org.datastore.*`), et il fallait connaître
    notre modèle de droits pour trouver « les routes des tableaux ».

    La ressource est le DEUXIÈME segment quand il existe (`me.datastore.append_row` →
    `datastore`), le premier sinon. ⚠️ Les clés plates gardent donc leur portée pour
    section — `admin.instance_health` reste sous `admin` — et c'est voulu : leur second
    segment est un VERBE, en faire une section produirait une section par action, pire
    que le classement qu'on remplace. Trente et une des trente-quatre capacités `admin`
    sont dans ce cas.
    """
    parts = [p for p in key.split(".") if p]
    if not parts:
        return "_"
    return parts[1] if len(parts) >= 3 else parts[0]


def _reponse_heureuse(Output, defs: dict, media: str = "application/json") -> dict:
    """La réponse heureuse : son schéma dès que la sortie est DÉCLARÉE (`Output`), ses
    sous-modèles hissés dans `defs`. Sans elle, on ne peut qu'annoncer « OK »."""
    ok: dict = {"description": "OK"}
    if Output is not None:
        try:
            out = Output.model_json_schema(ref_template="#/components/schemas/{model}")
        # noqa: SILENT — schéma de sortie illisible ⇒ document amputé, jamais absent
        except Exception:                              # modèle exotique → sans schéma
            out = {}
        if isinstance(out, dict):
            defs.update(out.pop("$defs", {}) or {})
        if out:
            ok = {"description": "OK", "content": {media: {"schema": out}}}
    elif media != "application/json":
        ok = {"description": "OK", "content": {media: {"schema": {"type": "string"}}}}
    return ok


def _operation(cap: Capability, binding: RestBinding,
               operation_id: str) -> tuple[dict, dict]:
    """Opération OpenAPI d'un binding + les définitions `$defs` à hisser.
    `operation_id` est attribué par `build`, qui seul voit les autres bindings."""
    try:
        schema = cap.Input.model_json_schema(ref_template="#/components/schemas/{model}")
    # noqa: SILENT — schéma d'entrée illisible ⇒ document amputé, jamais absent
    except Exception:                                  # modèle exotique → sans schéma
        schema = {}
    defs = schema.pop("$defs", {}) if isinstance(schema, dict) else {}
    props = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])

    # Les placeholders de chemin sont alimentés par le champ Input homonyme (ou
    # celui que `path_map` désigne) : ils sortent du corps pour devenir des params.
    field_of = {ph: (binding.path_map or {}).get(ph, ph)
                for ph in _placeholders(binding.path)}
    params = []
    for ph, field in field_of.items():
        params.append(_param(ph, "path", props.pop(field, None), True,
                             f"champ `{field}` de la requête" if field != ph else ""))
        required.discard(field)

    # La 200 porte un schéma dès que la capacité DÉCLARE sa sortie (`Output`). Sans
    # lui, on ne peut qu'annoncer « OK » — ce qui suffit à appeler, jamais à écrire
    # le client qui consomme. Cf. `Capability.Output` et le garde-fou de dette.
    ok = _reponse_heureuse(cap.Output, defs, binding.media)

    op: dict = {
        "operationId": operation_id,
        "summary": (cap.description or cap.key).strip().split(". ")[0][:180],
        "description": cap.description or "",
        "tags": [_section(cap.key)],
        "security": [{"bearerAuth": []}],
        # Le code de la réponse heureuse vient du binding (201 sur les créations
        # historiques) : le document décrit ce que le serveur REND, pas 200 par
        # convention — un client généré qui n'attend que 200 traiterait un 201
        # comme une erreur.
        "responses": _reponses(cap, (str(binding.status), ok)),
    }
    if binding.provisoire:
        # Forme ATTENDUE, pas contrat figé (convention proposée par le front, prise
        # telle quelle). Dire « provisoire » DANS le document est ce qui autorise à
        # servir tôt : sans la marque, une absence de mention se lit comme « gravé ».
        op["x-oto-provisoire"] = True
    if binding.verb in _BODY_VERBS or binding.reads_body:
        if binding.body_field:
            # Corps LIBRE : le corps entier est la valeur d'UN champ (`body_field`),
            # donc c'est le schéma de ce champ qu'on publie — pas un objet qui
            # l'envelopperait, ce que le fil ne porte jamais.
            body = props.pop(binding.body_field, None) or {"type": "object"}
            required.discard(binding.body_field)
            for name, sub in props.items():
                params.append(_param(name, "query", sub, name in required))
            op["parameters"] = params
            op["requestBody"] = {"required": True,
                                 "content": {"application/json": {"schema": body}}}
            return op, defs
        body = {"type": "object", "properties": props}
        if required:
            body["required"] = sorted(required)
        op["parameters"] = params
        op["requestBody"] = {"required": bool(required),
                             "content": {"application/json": {"schema": body}}}
    else:
        # GET/DELETE : l'adaptateur REST lit la query string (`_rest_adapter`).
        for name, sub in props.items():
            params.append(_param(name, "query", sub, name in required))
        op["parameters"] = params
    return op, defs


def _handwritten_operation_id(verb: str, path: str) -> str:
    """L'`operationId` d'un chemin qui ne peut pas porter celui d'une capacité — donc
    dérivé de son CHEMIN. Un seul endroit : routes écrites à la main, alias dépréciés
    et bindings secondaires d'une capacité (`_capability_operation_ids`) se
    ressemblent trop pour que deux recettes divergent."""
    corps = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{verb.lower()}_{corps}"


def _operation_de_nature(contrat: ContratDeRoute, verb: str, path: str,
                         params: list, defs: dict) -> dict:
    """L'opération d'une route de NATURE qui DÉCLARE son contrat (oto#106) : la même
    matière qu'une capacité — description, réponse heureuse, refus déclarés — plus son
    corps par verbe, et `security: []` quand le jeton de l'URL fait foi."""
    reponses = _fusionne_refus(
        {"200": _reponse_heureuse(contrat.Output, defs, contrat.media)}, contrat.errors)
    op = {
        "operationId": _handwritten_operation_id(verb, path),
        "summary": contrat.description.strip().split(". ")[0][:180],
        "description": contrat.description,
        "tags": [_TAG_NATURE],
        "security": [{"bearerAuth": []}] if contrat.authentifiee else [],
        # ⚠️ Les en-têtes de contexte RESTENT déclarés, jeton dans l'adresse ou non :
        # `ViewAsMiddleware` précède toute route `/api/*`, et le front les épingle sur
        # ces opérations depuis qu'elles étaient des souches — les retirer cassait son
        # contrat (préprod rouge, contrôle « Contrat avant déploiement »).
        "parameters": [*params, *_PARAMS_CONTEXTE],
        "responses": reponses,
    }
    corps = (contrat.corps or {}).get(verb.upper())
    if corps:
        # `required: False` : un corps devenu OBLIGATOIRE au contrat est une casse pour
        # un client généré sur la souche d'avant. Le serveur, lui, refuse toujours un
        # corps vide (`400 empty_body`, déclaré).
        op["requestBody"] = {"required": False, "content": {
            media: {"schema": schema} for media, schema in corps.items()}}
    return op


def _handwritten(routes: Iterable, defs: Optional[dict] = None, *,
                 admin: bool = False) -> dict:
    """Routes Starlette écrites à la main : chemin + méthodes, sans schéma.

    Les documenter sans corps vaut mieux que les taire — l'intégrateur sait au moins
    qu'elles existent, et qu'il faut demander leur forme. Elles décroissent au fil
    des migrations en capacités (`test_rest_modules_are_capabilities.py`).

    ⚠️ Une route de NATURE (qui ne deviendra jamais capacité) peut porter son contrat
    (`ContratDeRoute`, posé sur son handler) : elle est alors décrite pour de bon —
    sinon un client voit la porte sans pouvoir l'ouvrir (oto#106).
    """
    out: dict = {}
    defs = {} if defs is None else defs
    # ⚠️ Les alias DÉRIVÉS comptent autant que les déclarés. Oubliés ici le
    # 09/09/2026, les 24 chemins du renommage `namespace` → `datastore`
    # tombaient dans le chemin « route écrite à la main » et étaient décrits en
    # SOUCHES — sans paramètres, sans corps. Le contrôle de contrat des fronts,
    # qui apparie par chemin, lisait alors la souche à la place de l'opération et
    # concluait que six opérations avaient perdu leurs paramètres. Elles ne les
    # avaient pas perdus : c'est le document qui décrivait mal la redirection.
    alias = ({a.ancien for a in deprecations.REST}
             | {a.ancien for a in deprecations.alias_rest_derives()})
    for route in routes or ():
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or not path.startswith("/api/"):
            continue
        if not _du_document(path, admin):
            continue
        if path in alias:
            continue      # décrit par `_alias_deprecies` : « déprécié », pas « legacy »
        item = out.setdefault(_openapi_path(path), {})
        params = [_param(ph, "path", {"type": "string"}, True)
                  for ph in _placeholders(path)]
        contrat = getattr(getattr(route, "endpoint", None), "contrat", None)
        for verb in methods:
            v = verb.lower()
            if v in ("options", "head") or v in item:
                continue
            if isinstance(contrat, ContratDeRoute):
                item[v] = _operation_de_nature(contrat, verb, path, params, defs)
                continue
            item[v] = {
                "operationId": _handwritten_operation_id(v, path),
                "summary": f"{verb} {path}",
                "description": "Route écrite à la main : forme du corps non dérivable "
                               "(elle n'est pas encore une capacité).",
                "tags": ["_legacy"],
                "security": [{"bearerAuth": []}],
                "parameters": [*params, *_PARAMS_CONTEXTE],
                "responses": {"200": {"description": "OK"}},
            }
    return {p: i for p, i in out.items() if i}


def _alias_deprecies() -> dict:
    """Les anciens chemins, décrits comme ce qu'ils sont : **dépréciés et datés**.

    Ils sont DÉCLARÉS (`deprecations.REST`), pas relevés dans la table vivante :
    un intégrateur qui lit ce document doit y voir la date de retrait même quand
    le document est bâti sans table de routes.

    Trois choses que l'entrée porte et qu'un `deprecated: true` seul ne dirait pas :
    le chemin de REMPLACEMENT (sinon « déprécié » n'est qu'un reproche), la DATE
    (sinon rien ne dit quand agir), et l'`operationId` HISTORIQUE — le nom de méthode
    qu'un client généré s'est donné, qu'on ne fait pas changer pour rien.
    """
    out: dict = {}
    for alias in tuple(deprecations.REST) + tuple(deprecations.alias_rest_derives()):
        item = out.setdefault(_openapi_path(alias.ancien), {})
        item[alias.verbe.lower()] = {
            "operationId": alias.operation_id or _handwritten_operation_id(
                alias.verbe, alias.ancien),
            "summary": f"Déprécié : utilisez {alias.nouveau} "
                       f"(retrait le {deprecations._sunset(alias)})",
            "description": (
                f"Ancien chemin, conservé le temps du préavis. Il répond **308** vers "
                f"`{alias.nouveau}` — même méthode, même corps, query string reportée "
                f"— et **cesse de répondre au premier tag posé à partir du "
                f"{deprecations._sunset(alias)}**. Bascule sur le nouveau chemin : "
                f"il sert déjà, à l'identique."),
            "deprecated": True,
            "tags": ["_deprecated"],
            "security": [{"bearerAuth": []}],
            "parameters": [_param(ph, "path", {"type": "string"}, True)
                           for ph in _placeholders(alias.ancien)],
            "responses": {
                "308": {"description": f"Redirection permanente vers {alias.nouveau}",
                        "headers": {
                            "Location": {"schema": {"type": "string"}},
                            "Sunset": {"description": "date de retrait (JJ/MM/AAAA)",
                                       "schema": {"type": "string"}},
                        }},
            },
        }
    return out


def _capability_operation_ids(cap: Capability, bindings: list) -> list[str]:
    """Un `operationId` par binding publié, UNIQUE (#436).

    L'id suit la CAPACITÉ (`{clé}_{verbe}`, cf. `docs/alias-deprecies.md`) — mais une
    capacité à plusieurs bindings du même verbe (`me.guides.get` sur `/api/me/…`,
    `/api/orgs/{id}/…`, `/api/groups/{id}/…`) le donnait à ses trois chemins, et un
    client généré, indexé sur l'id, en perdait deux en silence. Le PREMIER binding
    déclaré du verbe garde l'id de la capacité — celui que les clients déjà générés
    appellent — et les suivants reçoivent un id dérivé de leur chemin."""
    ids: list[str] = []
    verbes_pris: set[str] = set()
    for binding in bindings:
        verbe = binding.verb.lower()
        if verbe in verbes_pris:
            ids.append(_handwritten_operation_id(verbe, binding.path))
        else:
            verbes_pris.add(verbe)
            ids.append(f"{cap.key}.{verbe}".replace(".", "_"))
    return ids


def build(routes: Optional[Iterable] = None, *, server_url: Optional[str] = None,
          admin: bool = False) -> dict:
    """Document OpenAPI 3.1 complet. `routes` = table de routes vivante (facultative :
    sans elle, seules les capacités sont décrites).

    `admin=False` : le document PUBLIC, sans `/api/admin/*`. `admin=True` : le document
    d'ADMINISTRATION, `/api/admin/*` seul et entier (#1196)."""
    schemas: dict = {"Erreur": _ERREUR}
    paths = _handwritten(routes, schemas, admin=admin)
    for chemin, item in _alias_deprecies().items():
        if _du_document(chemin, admin):
            paths.setdefault(chemin, {}).update(item)
    for cap in registry.CAPABILITIES:
        if not cap.is_exposed():
            continue
        publies = [b for b in cap.rest_bindings() if _du_document(b.path, admin)]
        for binding, operation_id in zip(publies, _capability_operation_ids(cap, publies)):
            op, defs = _operation(cap, binding, operation_id)
            # L'en-tête de run : LA passe qui le référence, sur les capacités et elles
            # seules ; les en-têtes de contexte, lus avant toute route `/api/*`, aussi.
            op["parameters"] = [*op.get("parameters", []), *_PARAMS_CONTEXTE, _PARAM_RUN_REF]
            schemas.update(defs)
            item = paths.setdefault(_openapi_path(binding.path), {})
            item[binding.verb.lower()] = op          # la capacité prime sur le legacy
    # Noms de schéma d'HIER (#519) : un `$ref` vers celui d'aujourd'hui, marqué
    # déprécié. Un client généré qui vise l'ancien nom continue de résoudre jusqu'au
    # retrait — un `$ref` cassé, lui, fait échouer la génération ENTIÈRE du client,
    # pas seulement le modèle visé.
    for ancien, actuel in deprecations.SCHEMAS.items():
        if actuel in schemas and ancien not in schemas:
            schemas[ancien] = {
                "$ref": f"#/components/schemas/{actuel}",
                "deprecated": True,
                "description": f"Déprécié : utilisez `{actuel}` (retrait le "
                               f"{deprecations.date_de_retrait()}).",
            }
    doc = {
        "openapi": "3.1.0",
        # `version` = la version SERVIE (oto#33), pas un numéro de contrat. Le
        # document est dérivé du serveur qui le sert : le figer à « 1 » revenait
        # à publier une carte sans dire de quel jour elle date — et un intégrateur
        # qui constate une dérive de forme n'avait rien pour la situer. Même
        # étiquette que l'en-tête `X-Oto-Version` et que `GET /api/version`, pour
        # qu'un descriptif et un journal d'appels se recoupent sans traduction.
        "info": {"title": _TITLE_ADMIN if admin else _TITLE,
                 "version": version.version_servie(),
                 "description": _DESCRIPTION_ADMIN if admin else _DESCRIPTION},
        "paths": dict(sorted(paths.items())),
        "components": {
            "parameters": _parametres(),
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer",
                               "description": "JWT Logto ou jeton API `oto_…`"},
            },
            "schemas": schemas,
        },
        "security": [{"bearerAuth": []}],
    }
    if server_url:
        doc["servers"] = [{"url": server_url}]
    return doc
