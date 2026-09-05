"""HubSpot CRM — contacts, companies, deals, tickets, notes (read + write).

Wrappe `oto.tools.hubspot.HubSpotClient` (private app token). Clé résolue par
appel via `access.resolve_api_key("hubspot")` — byo (user key sur /account ou
credential partagé de l'org). Pas de clé plateforme.

Surface générique : `object_type` = contacts | companies | deals | tickets
(ou tout objet custom) pour search/get/create/update/delete — fusion sans perte.

**Surface consolidée (ADR 0047 §Amendement)** : 9 tools → 2. Les HUIT verbes qui
portaient `object_type` (`search`/`get`/`list`/`create`/`update`/`delete`/
`associations`/`create_note`) vivent dans **`hubspot_object`**, le verbe en `op` —
ils partageaient déjà leurs paramètres (`object_type`, `object_id`, `properties`),
et c'est ÇA le critère de fusion, pas le comptage. **`hubspot_owners` reste SEUL** :
il ne prend aucun paramètre d'objet CRM (ni `object_type`, ni `object_id`, ni
`properties`) et lit un référentiel d'utilisateurs, pas un enregistrement — le
fusionner n'aurait factorisé aucun paramètre, donc pesé autant que deux tools.

⚠️ Deux paramètres sont des HOMONYMES dont le type dépend de l'`op` — c'est le prix
de la fusion, et il est payé par une validation DURE (jamais une coercition ni un
fallback silencieux : la mauvaise forme lève ici plutôt que de partir chez HubSpot
qui répondrait un 400 opaque) :
- `properties` = list[str] (noms de propriétés à RETOURNER) en lecture
  (search/list/get) ; dict {propriété: valeur} à ÉCRIRE en écriture
  (create/update).
- `associations` = list[str] (types d'objets dont on veut les ids liés) sur
  op="get" ; list[dict] (objets d'association HubSpot v3) sur op="create".

**`hubspot_list` — les « segments »**. Les listes HubSpot SONT le mécanisme de
segmentation (la doc les décrit comme servant au « record segmentation »), il n'y
a pas d'API `segments` séparée. Deux pièges structurels, traités ici et pas chez
l'agent :
1. Les listes sont keyées sur un `objectTypeId` NUMÉRIQUE (`0-1` contacts, `0-2`
   companies, `0-3` deals, `0-5` tickets, `2-<n>` custom) là où tout le reste du
   connecteur parle en `"contacts"`. On accepte le nom ET l'id brut, on traduit.
2. Une liste `DYNAMIC` REFUSE les écritures d'appartenance (ses membres sont
   recalculés depuis ses critères). On lit son `processingType` AVANT d'écrire
   pour rendre un message actionnable, plutôt que de laisser partir un 400 opaque.

**`filterBranch` est un passe-plat assumé** : l'arbre de critères HubSpot est
récursif (`filterBranchType` OR/AND/UNIFIED_EVENTS/ASSOCIATION, forme d'`operation`
par `filterType`) — le modéliser coûterait une page de schéma pour peu de gain. On
le transmet tel quel, en dict, documenté comme avancé. C'est `hubspot_property` qui
rend ce passe-plat utilisable : un `filterBranch` référence des propriétés par NOM
INTERNE (`dealstage`, pas « Deal stage ») et les listes déroulantes n'acceptent que
leurs `options[].value` — sans ce référentiel, tout critère (et tout create/update)
est une devinette.

3. Une appartenance ne porte QUE `recordId`. Lire sept colonnes par membre
   coûtait donc un `hubspot_object op='get'` PAR membre — un N+1 qui, à quatre
   appels par lead, tape le plafond d'une private app (190 requêtes / 10 s) vers
   la quarantième fiche, et un 429 non rattrapé arrête le run au milieu d'un
   enregistrement à moitié écrit. `op='members'` accepte donc `properties` : il
   compose la page d'appartenances avec UN batch read (`batch_read_objects`,
   tranché à 100 par le client) et rend des lignes complètes. Sans `properties`,
   l'op répond exactement ce qu'elle a toujours répondu, en un seul appel.

⚠️ **Scopes** : les listes exigent `crm.lists.read` / `crm.lists.write` dans la
private app. Les tokens créés avant ces tools n'ont que les scopes `crm.objects.*`
→ un 403 ici veut dire « ajoute le scope », PAS « clé invalide ».
"""
from __future__ import annotations

import re
from typing import Literal, Optional, Union

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


#: Les clés que `batch_read_objects` rend TOUJOURS, toutes les deux, jamais à
#: None (contrat oto-core). Écrites une fois, ici : c'est la seule description
#: de la forme du client dans ce dépôt, et le refus ci-dessous s'y adosse.
_CLES_ENVELOPPE = ("results", "missing_ids")


def _batch_read_envelope(lecture) -> tuple:
    """Ouvre l'ENVELOPPE de `batch_read_objects` — un mapping, pas une liste.

    oto-core rend `{"results": [...], "missing_ids": [...]}`. `missing_ids` est
    le relevé des ids demandés que HubSpot n'a pas rendus : son batch read
    répond 207 sans nommer les absents, et le client est le SEUL endroit où cet
    écart est calculé. On le rend au site d'appel plutôt que de le laisser
    tomber — une page de 250 membres qui revient à 247 lignes doit s'annoncer.

    ⚠️ Le refus est la raison d'être de cette fonction, et il porte sur les DEUX
    clés NOMMÉMENT, pas seulement sur le type du contenant :

    - un client qui rend une LISTE nue (la forme d'avant le tag) : la prendre
      pour l'enveloppe itérerait ses CLÉS (`"results"`, une chaîne) et lèverait
      un `AttributeError` opaque au fond du recollage, plusieurs frames plus
      loin ;
    - un client qui RENOMME `missing_ids` : c'est l'accident silencieux, et le
      plus grave des deux. Lire la clé avec un défaut (`.get(…) or []`) rendrait
      une enveloppe amputée SANS un mot — la page de 250 membres revenue à 247
      s'annoncerait vide de tout écart, ce qui est très exactement le succès
      déguisé que ce relevé existe pour interdire. On exige donc les deux clés
      par leur nom : une dérive inter-dépôts devient un refus, jamais une page
      rétrécie en silence.

    C'est ici, et nulle part ailleurs, que la FORME du client est écrite :
    `tests/test_tools_client_methods_exist.py` prouve que la MÉTHODE existe sur
    le tag épinglé, rien ne prouve mécaniquement ce qu'elle rend.

    Rend `(results, missing_ids)`. La forme de `results` n'est pas jugée ici :
    c'est `_rows_from_memberships` qui la refuse, à son tour et par son nom.
    """
    if not isinstance(lecture, dict):
        raise TypeError(
            "batch_read_objects doit rendre l'enveloppe "
            "{'results': [...], 'missing_ids': [...]} et non "
            f"{type(lecture).__name__} — pin oto-core en retard sur le tag qui "
            "porte cette forme (cf. pyproject.toml)")
    defaillantes = [k for k in _CLES_ENVELOPPE if lecture.get(k) is None]
    if defaillantes:
        raise TypeError(
            "batch_read_objects doit rendre l'enveloppe "
            "{'results': [...], 'missing_ids': [...]} : "
            f"clé(s) absente(s) ou nulle(s) {defaillantes}, reçu les clés "
            f"{sorted(lecture)} — pin oto-core en retard, ou clé renommée côté "
            "client (cf. pyproject.toml). Servir la page sans `missing_ids` la "
            "rétrécirait sans le dire.")
    return lecture["results"], list(lecture["missing_ids"])


def _missing_report(rows, missing_ids) -> dict:
    """Les clés d'écart à servir — DEUX verdicts sur le même fait, jamais fondus.

    `missing_ids` est le verdict du CLIENT (les ids qu'il a demandés et que
    HubSpot n'a pas rendus) ; `missing_count` est celui de la JOINTURE (les
    lignes servies sans `properties`). Ils doivent coïncider. On sert les deux
    plutôt qu'un seul, et on NOMME leur désaccord au lieu d'en choisir un en
    silence : le jour où ils divergent, c'est que l'un des deux a tort, et c'est
    précisément le genre d'écart qu'on refuse de laisser passer sans un mot.

    ⚠️ **Le désaccord est SYMÉTRIQUE, et il le devient parce que l'autre sens
    s'est produit.** Une première version ne comparait les deux ensembles que si
    le client avait, lui, quelque chose à dire (`if missing_ids and …`) : une
    absence vue par la seule JOINTURE — une appartenance sans `recordId`, donc
    un id jamais demandé au batch read, qui ne peut par construction pas figurer
    dans `missing_ids` — servait alors `missing_count: 1` tout seul, sans un id
    ni une phrase pour dire de qui on parle. Un chiffre sans nom est le pire des
    deux mondes : assez visible pour inquiéter, trop muet pour agir. La
    comparaison porte donc sur les deux ensembles, dans les deux sens, dès que
    l'un des deux n'est pas vide.

    Pure, et donc exerçable directement.
    """
    absents_du_join = [str(r.get("recordId")) for r in rows if "missing" in r]
    reportes = [str(i) for i in (missing_ids or [])]
    out: dict = {}
    if reportes:
        out["missing_ids"] = list(missing_ids)
    if absents_du_join:
        out["missing_count"] = len(absents_du_join)
    if set(reportes) != set(absents_du_join):
        out["missing_mismatch"] = {
            "reported_by_client": list(missing_ids or []),
            "absent_from_join": absents_du_join,
            "note": ("the batch read's own verdict and the membership join "
                     "disagree on which records are missing"),
        }
    return out


def _rows_from_memberships(memberships, records, properties=None) -> list[dict]:
    """Recolle une page d'appartenances à ses enregistrements, dans l'ORDRE de la page.

    Une appartenance ne porte que `recordId` ; les colonnes viennent d'un batch
    read séparé, qui peut en rendre MOINS (enregistrement supprimé entre les deux
    appels, ou hors des droits de cette clé). L'écart est NOMMÉ (`properties:
    None` + `missing`) plutôt que comblé ou tu : une ligne muette au milieu d'une
    population de prospection est exactement ce qu'on ne veut pas fabriquer.

    On itère les APPARTENANCES, jamais les enregistrements — c'est ce qui garde
    l'ordre de la page et interdit de perdre un membre en chemin. Chaque ligne
    part de l'appartenance TELLE QUELLE (`recordId` n'est pas renommé) : une
    procédure qui lit déjà `results[].recordId` continue de marcher le jour où
    elle se met à passer `properties`.

    Pure (ni client, ni contexte, ni fermeture) et donc au niveau module, à la
    différence des helpers de `register()` : la forme des lignes est ce qu'on
    veut pouvoir exercer directement.

    `records` est la LISTE `results` du batch read, pas la valeur rendue par
    `batch_read_objects` (qui est une enveloppe) : l'ouvrir est le travail de
    `_batch_read_envelope`, au site d'appel qui connaît le contrat du client.
    Une autre forme se REFUSE ici plutôt que de s'itérer — un dict s'itère sur
    ses clés et rendrait `AttributeError: 'str' object has no attribute 'get'`,
    une panne opaque là où il faut un nom.

    Les clés AJOUTÉES sont en anglais, comme le reste de la surface servie de ce
    tool ; les refus levés, eux, restent en français comme leurs voisins `_bad`.
    """
    if records is None:
        records = []
    if not isinstance(records, list):
        raise TypeError(
            "_rows_from_memberships attend la LISTE `results` du batch read, "
            f"pas {type(records).__name__} : `batch_read_objects` rend "
            "l'enveloppe {'results': [...], 'missing_ids': [...]}, ouverte au "
            "site d'appel par _batch_read_envelope")
    by_id = {str(r.get("id")): r for r in records}
    rows: list[dict] = []
    for m in memberships or []:
        row = dict(m)  # l'appartenance HubSpot VERBATIM (recordId, timestamp, …)
        rec = by_id.get(str(m.get("recordId")))
        row["properties"] = rec.get("properties") if rec else None
        if rec is None:
            row["missing"] = ("record not returned by the batch read "
                              "(deleted, or outside this key's scope)")
        elif properties:
            absent = [p for p in properties
                      if p not in (rec.get("properties") or {})]
            if absent:
                # Sépare « HubSpot n'a pas de valeur » de « ce nom interne
                # n'existe pas » : sans ça, un nom mal orthographié se lit comme
                # une colonne vide — et les noms internes sont très exactement
                # ce que `hubspot_property` existe pour donner.
                row["missing_properties"] = absent
        rows.append(row)
    return rows


# HubSpot rend un 403 `MISSING_SCOPES` dont le message — « The scope needed for this
# API call isn't available for public use » — se lit comme « ce scope ne t'est pas
# accessible ». C'est FAUX pour les objets qu'on sert : le scope `tickets` est
# documenté « Available to all accounts », il se coche dans l'app privée. Le corps brut
# partait tel quel à l'agent, qui n'avait aucune raison d'y voir une case à cocher chez
# le client : le même signal a été redéposé À L'IDENTIQUE deux jours de suite par la
# même procédure quotidienne (#636 puis #649). On NOMME donc le geste.
def _scope_refusal(e, object_type) -> Optional[McpError]:
    """Le 403 MISSING_SCOPES traduit en refus actionnable, ou None si autre chose."""
    if getattr(e, "status_code", None) != 403:
        return None
    body = getattr(e, "body", None)
    if not (isinstance(body, dict) and body.get("category") == "MISSING_SCOPES"):
        return None
    return McpError(ErrorData(code=INVALID_PARAMS, message=(
        f"HubSpot refuse cette lecture faute de scope sur le jeton "
        f"(403 MISSING_SCOPES, object_type={object_type!r}). Son message « isn't "
        "available for public use » est trompeur : le scope existe et se coche. "
        "Côté HubSpot : Settings > Integrations > Private Apps > l'app qui porte ce "
        "jeton > onglet Scopes, activer celui de cet objet (`tickets` pour les "
        "tickets, `crm.objects.*` pour contacts/entreprises/transactions, "
        "`crm.lists.*` pour les listes), puis relire le jeton. Rien à corriger dans "
        "l'appel : les autres objets répondent avec la MÊME clé.")))


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /account-info/v3/details`. Ce que la doc HubSpot établit :

    - **authentifié** — Bearer token (jeton d'app privée), comme le reste de
      l'API ;
    - **sans effet de bord** — une lecture de compte (`portalId`, `accountType`,
      `timeZone`…) ;
    - **le coût** — aucune mention de coût ni de limite de débit particulière
      pour cet appel. Absence de mention, indice, pas une preuve.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue PAS ici — cet
    appel ne révèle aucun scope, et HubSpot les accorde OBJET PAR OBJET
    (`crm.objects.contacts.*`, `tickets`…, cf. `_scope_refusal` ci-dessus) :
    un jeton peut lire les contacts et pas les tickets, ce qui n'est PAS un état
    « connecteur mort », c'est un manque LOCAL à un objet (403 `MISSING_SCOPES`
    déjà traduit à l'appel réel). Troisième règle d'oto#69 : un scope partiel ne
    se mesure pas dans le verdict de connexion.
    """
    from oto.tools.hubspot.client import HubSpotClient

    infos = HubSpotClient(api_key=fields["key"])._request(
        "GET", "/account-info/v3/details") or {}
    if not infos.get("portalId"):
        raise RuntimeError(
            "HubSpot a répondu sans identifier de compte pour cette clé — "
            f"réponse inattendue : {str(infos)[:200]}")


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError

    from oto.tools.hubspot.client import HubSpotClient

    connector_verify.register("hubspot", _verify)

    def _client() -> HubSpotClient:
        key, _ = access.resolve_api_key("hubspot")
        return HubSpotClient(api_key=key)

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _need(value, name: str, op: str):
        """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback."""
        if value is None:
            raise _bad(f"op='{op}' requiert {name}")
        return value

    def _names(value, name: str, op: str) -> Optional[list]:
        """Forme LECTURE d'un paramètre homonyme : une liste de NOMS (list[str]).

        `properties` et `associations` changent de type selon l'op (cf. docstring
        du module) : on refuse ici la forme d'écriture au lieu de la transmettre.
        """
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _bad(
                f"op='{op}' attend {name} = liste de noms de propriétés (list[str]) ; "
                "la forme dict/objets est celle des op d'écriture")
        return value

    def _payload(value, name: str, op: str) -> dict:
        """Forme ÉCRITURE de `properties` : un dict {propriété: valeur}."""
        _need(value, name, op)
        if not isinstance(value, dict):
            raise _bad(
                f"op='{op}' attend {name} = dict {{propriété: valeur}} ; la liste de "
                "noms est la forme des op de lecture")
        return value

    def _assoc_objects(value, op: str) -> Optional[list]:
        """Forme ÉCRITURE d'`associations` : objets d'association HubSpot v3."""
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise _bad(
                f"op='{op}' attend associations = liste d'objets d'association "
                "HubSpot v3 (list[dict]) ; la liste de types est la forme d'op='get'")
        return value

    @mcp.tool()
    def hubspot_object(
        op: Literal["search", "list", "get", "create", "update", "delete",
                    "associations", "add_note"] = "search",
        object_type: Optional[str] = None,
        object_id: Optional[str] = None,
        properties: Optional[Union[list[str], dict]] = None,
        associations: Optional[Union[list[str], list[dict]]] = None,
        query: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        to_object_type: Optional[str] = None,
        body: Optional[str] = None,
        limit: int = 100,
        after: Optional[str] = None,
    ) -> dict:
        """HubSpot CRM objects — one tool, the verb in `op`.

        `object_type` = contacts | companies | deals | tickets (or any custom
        object) drives every op, and is always required.

        Ops:
        - **"search"** (default) : Search CRM objects. Full-text via `query`,
          structured via `filters`. Paginated (`limit`, `after`).
        - **"list"** : List CRM objects of a type (paginated via `after`).
        - **"get"** : Fetch one CRM object by id. Requires `object_id`.
        - **"create"** : Create a CRM object. Requires `properties` (dict).
        - **"update"** : Update (PATCH) a CRM object's properties. Requires
          `object_id` + `properties` (dict).
        - **"delete"** : Archive a CRM object (moves it to HubSpot's recycle bin).
          Requires `object_id`.
        - **"associations"** : List objects of `to_object_type` associated with an
          object. e.g. the deals of a contact: object_type="contacts",
          to_object_type="deals". Requires `object_id` + `to_object_type`.
        - **"add_note"** : Attach a note to a CRM object (contacts/companies/deals/
          tickets). Requires `body` + `object_id` (the object the note hangs on).

        ⚠️ `properties` and `associations` are HOMONYMS whose expected type depends
        on `op` (read = list of names, write = dict / association objects) — see the
        Args below. A wrong shape is refused with an explicit error, never coerced.

        Args:
            op: search | list | get | create | update | delete | associations |
                add_note.
            object_type: contacts | companies | deals | tickets (or custom).
                Required for every op ; on op="add_note" it is the type of the
                object the note is attached to.
            object_id: id of the object — required for get, update, delete,
                associations and add_note.
            properties:
                - READ (search, list, get) : property names to return (list[str]).
                - WRITE (create, update) : object properties (dict), e.g.
                  {"email": …, "firstname": …} for a contact ; {"dealname": …,
                  "amount": …} for a deal.
            associations:
                - op="get" : other object types to return associated ids for
                  (e.g. ["companies", "deals"] on a contact) — this returns the
                  ids INLINE, so it replaces a separate op="associations" round
                  trip per object.
                - op="create" : HubSpot v3 association objects (advanced).
            query: op="search" — full-text search.
            filters: op="search" — list of {propertyName, operator, value} combined
                with AND. Passed to HubSpot VERBATIM — nothing here validates the
                operator, so HubSpot's own set is the reference: EQ, NEQ, LT, LTE,
                GT, GTE, BETWEEN (add "highValue"), IN / NOT_IN (pass "values":
                [...] instead of "value"), HAS_PROPERTY, NOT_HAS_PROPERTY,
                CONTAINS_TOKEN, NOT_CONTAINS_TOKEN (wildcards `*`).
            to_object_type: op="associations" — the associated object type to list.
            body: op="add_note" — the note content (text/HTML).
            limit: op="search"/"list" — page size (HubSpot caps it at 100).
            after: op="search"/"list" — pagination cursor from a previous response
                (paging.next.after).
        """
        c = _client()

        try:
            if op == "search":
                return c.search_objects(
                    _need(object_type, "object_type", op),
                    query=query, filters=filters,
                    properties=_names(properties, "properties", op),
                    limit=limit, after=after)

            if op == "list":
                return c.list_objects(
                    _need(object_type, "object_type", op),
                    properties=_names(properties, "properties", op),
                    limit=limit, after=after)

            if op == "get":
                return c.get_object(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op),
                    properties=_names(properties, "properties", op),
                    associations=_names(associations, "associations", op))

            if op == "create":
                return c.create_object(
                    _need(object_type, "object_type", op),
                    _payload(properties, "properties", op),
                    associations=_assoc_objects(associations, op))

            if op == "update":
                return c.update_object(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op),
                    _payload(properties, "properties", op))

            if op == "delete":
                return c.delete_object(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op))

            if op == "associations":
                return c.list_associations(
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op),
                    _need(to_object_type, "to_object_type", op))

            if op == "add_note":
                return c.create_note(
                    _need(body, "body", op),
                    _need(object_type, "object_type", op),
                    _need(object_id, "object_id", op))

            raise _bad("op doit être 'search', 'list', 'get', 'create', 'update', "
                       "'delete', 'associations' ou 'add_note'")
        except UpstreamHTTPError as e:
            refus = _scope_refusal(e, object_type)
            if refus is None:
                raise          # tout autre refus amont garde sa forme et sa trace
            raise refus from None

    # objectTypeId : les listes sont keyées sur l'id numérique, pas sur le nom
    # d'objet. On accepte les deux — le nom pour les quatre standard, l'id brut
    # `N-N` pour tout le reste (aucune table ne peut couvrir les objets custom,
    # dont l'id dépend du portail).
    _OBJECT_TYPE_IDS = {
        "contacts": "0-1", "companies": "0-2", "deals": "0-3", "tickets": "0-5",
    }

    # L'INVERSE de la table ci-dessus : une liste porte son `objectTypeId`, mais
    # l'endpoint de batch read se keye, lui, sur le nom d'objet.
    _LIST_OBJECT_NAMES = {v: k for k, v in _OBJECT_TYPE_IDS.items()}

    def _object_type_id(value, op: str) -> str:
        """Traduit `object_type` en `objectTypeId` HubSpot pour les listes."""
        _need(value, "object_type", op)
        key = str(value).strip().lower()
        if key in _OBJECT_TYPE_IDS:
            return _OBJECT_TYPE_IDS[key]
        if re.fullmatch(r"\d+-\d+", key):
            return key  # id brut (objet custom : `2-<n>`)
        raise _bad(
            f"object_type='{value}' inconnu pour les listes : attendu "
            "contacts | companies | deals | tickets, ou l'objectTypeId brut "
            "d'un objet custom (forme '2-7', lisible dans les réglages HubSpot)")

    def _ids(value, name: str, op: str) -> list:
        """Liste d'ids d'enregistrements — HubSpot les veut en chaînes."""
        _need(value, name, op)
        if not isinstance(value, list) or not value:
            raise _bad(f"op='{op}' attend {name} = liste non vide d'ids")
        return [str(v) for v in value]

    def _batch_object_type(c, list_id: str, object_type, op: str) -> str:
        """De quel type d'objet sont les membres — DÉRIVÉ, jamais deviné.

        Une appartenance ne porte que `recordId` ; le batch read, lui, est keyé
        par type d'objet. `op='members'` ayant toujours pris `list_id` SEUL, on
        va lire le type sur la fiche de la liste (un GET, celui-là même que
        `_writable_list` fait déjà avant d'écrire) au lieu d'exiger un argument
        neuf sur une op qui a déjà des appelants. Passer `object_type`
        explicitement économise ce GET.

        Un type indevinable se REFUSE, en nommant l'argument qui le donnerait :
        retomber sur « contacts » lirait le mauvais objet et rendrait une
        population plausible et fausse.
        """
        if object_type is not None:
            key = str(object_type).strip().lower()
            _object_type_id(key, op)  # valide la forme, refuse un type inconnu
            return key
        fiche = c.get_list(list_id)
        info = fiche.get("list") or fiche
        type_id = info.get("objectTypeId")
        if not type_id:
            raise _bad(
                f"op='{op}' avec properties : impossible de déterminer le type "
                f"d'objet des membres de la liste {list_id} (sa fiche ne porte "
                "pas d'objectTypeId) — passe object_type (contacts | companies "
                "| deals | tickets, ou l'objectTypeId brut d'un objet custom).")
        return _LIST_OBJECT_NAMES.get(str(type_id), str(type_id))

    def _writable_list(c, list_id: str, op: str) -> dict:
        """Charge la liste et REFUSE d'écrire ses membres si elle est DYNAMIC.

        Une liste dynamique recalcule ses membres depuis ses critères ; HubSpot
        répond un 400 générique sur les endpoints d'appartenance. Un GET
        préalable coûte peu et permet de dire quoi faire à la place — et sert
        aussi d'état « avant » pour les dry_run.
        """
        current = c.get_list(list_id)
        info = current.get("list") or current
        if info.get("processingType") == "DYNAMIC":
            raise _bad(
                f"op='{op}' impossible : la liste {list_id} "
                f"(« {info.get('name')} ») est DYNAMIC — ses membres sont "
                "recalculés par HubSpot. Change ses critères "
                "(op='update' avec filter_branch), pas ses membres.")
        return info

    @mcp.tool()
    def hubspot_list(
        op: Literal["search", "get", "create", "update", "delete", "restore",
                    "members", "add_members", "remove_members", "clear_members",
                    "copy_from", "record_lists"] = "search",
        list_id: Optional[str] = None,
        object_type: Optional[str] = None,
        name: Optional[str] = None,
        processing_type: Literal["MANUAL", "DYNAMIC", "SNAPSHOT"] = "MANUAL",
        filter_branch: Optional[dict] = None,
        record_ids: Optional[list] = None,
        remove_record_ids: Optional[list] = None,
        record_id: Optional[str] = None,
        source_list_id: Optional[str] = None,
        query: Optional[str] = None,
        include_filters: bool = False,
        limit: int = 100,
        after: Optional[str] = None,
        properties: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """HubSpot lists — the segments of a HubSpot portal. One tool, verb in `op`.

        A HubSpot "segment" IS a list: there is no separate segments API. Three
        kinds, set at creation and NOT changeable afterwards:
        - **MANUAL** : you decide who is in it (the `*_members` ops below).
        - **DYNAMIC** : HubSpot recomputes membership from `filter_branch`. Its
          membership ops are REFUSED — change the criteria instead.
        - **SNAPSHOT** : filtered once at creation, then managed by hand.

        `object_type` takes the usual name (contacts | companies | deals |
        tickets) and is translated to the numeric objectTypeId lists key on. For
        a custom object, pass its raw id (`"2-7"`).

        Ops:
        - **"search"** (default) : find lists by `name` fragment via `query`,
          optionally narrowed by `object_type`.
        - **"get"** : one list, by `list_id` — or by `name` + `object_type`.
          `include_filters=true` also returns its criteria tree.
        - **"create"** : create a list. Requires `name` + `object_type`. For a
          DYNAMIC/SNAPSHOT list, pass `filter_branch`.
        - **"update"** : rename (`name`) and/or replace the criteria
          (`filter_branch`). Requires `list_id`.
        - **"delete"** : delete a list — restorable for 90 days. Requires
          `list_id`. Supports `dry_run`.
        - **"restore"** : restore a deleted list (within those 90 days).
        - **"members"** : the record ids in a list (paginated, `after`). Pass
          `properties` to get FULL ROWS instead of bare ids — one memberships
          page plus ONE batch read, so a 100-record page costs 2 calls instead
          of the 101 it costs to follow up with `hubspot_object op="get"` per
          member. Do that: a HubSpot private app is capped at 190 requests per
          10 seconds, and the per-member loop hits the ceiling around the
          fortieth lead, mid-record.
        - **"add_members"** / **"remove_members"** : add/remove `record_ids`.
          On op="add_members", also passing `remove_record_ids` does both in a
          SINGLE list revision (one recompute instead of two).
        - **"clear_members"** : remove EVERY record from the list (the list
          survives). Supports `dry_run` — use it.
        - **"copy_from"** : copy every member of `source_list_id` into
          `list_id` (HubSpot caps this at 100 000 records).
        - **"record_lists"** : which lists a given record belongs to. Requires
          `object_type` + `record_id`.

        ⚠️ Requires the `crm.lists.read` / `crm.lists.write` scopes on the
        private app. A token created before these scopes existed answers 403 —
        that means "add the scope", not "the key is wrong".

        Args:
            op: search | get | create | update | delete | restore | members |
                add_members | remove_members | clear_members | copy_from |
                record_lists.
            list_id: the list — required for every op except search, create and
                record_lists.
            object_type: contacts | companies | deals | tickets, or a raw
                objectTypeId ("2-7") for a custom object. On op="members" with
                `properties`, it is the type the members are read as — omitted,
                it is derived from the list itself (one extra GET).
            name: op="create" the list name ; op="update" the new name ;
                op="get" look the list up by name (with `object_type`).
            processing_type: op="create" — MANUAL (default) | DYNAMIC | SNAPSHOT.
            filter_branch: op="create"/"update" — HubSpot's criteria tree, passed
                through verbatim. Recursive shape: {"filterBranchType": "OR",
                "filterBranches": [{"filterBranchType": "AND", "filters": [
                {"filterType": "PROPERTY", "property": "<internal name>",
                "operation": {"operationType": "NUMBER", "operator":
                "IS_GREATER_THAN_OR_EQUAL_TO", "value": 12}}]}]}. Property names
                are the INTERNAL ones — get them from `hubspot_property`.
            record_ids: op="add_members"/"remove_members" — the record ids to
                add / to remove.
            remove_record_ids: op="add_members" only — ids to remove in the same
                revision as the ones being added.
            record_id: op="record_lists" — the single record to look up.
            source_list_id: op="copy_from" — the list to copy members from.
            query: op="search" — name fragment.
            include_filters: op="get" — also return the list's criteria tree.
            limit: op="members" — page size.
            after: op="members" — pagination cursor from a previous response.
            properties: op="members" ONLY — the INTERNAL property names to
                return for each member (get them from `hubspot_property`;
                "Deal stage" is not one). Each row then carries the membership
                keys it already had plus `properties`. A member the batch read
                did not return keeps its row, with `properties: null` and a
                `missing` reason — rows are never dropped. A name absent from a
                returned record is listed in that row's `missing_properties`,
                which is how a typo tells itself apart from an empty column.
                The answer also carries `missing_ids` (the batch read's own
                verdict on what it could not fetch) and `missing_count`, when
                either is non-empty. Omitted, the op answers exactly what it
                always did: ids only. An EMPTY list is refused, not treated as
                "omitted" — it would cost two extra calls to return HubSpot's
                default columns, which is nobody's request.
            dry_run: op="delete"/"clear_members"/"remove_members" — validate and
                report what WOULD change (with the list's current state), without
                writing.
        """
        c = _client()

        # `properties` ne veut rien dire ailleurs que sur op='members' : le
        # taire serait une divergence MUETTE — l'appelant croirait avoir demandé
        # des colonnes et lirait un résultat qui n'en porte pas.
        if properties is not None and op != "members":
            raise _bad(
                f"op='{op}' n'accepte pas properties : la projection des "
                "colonnes n'existe que sur op='members' (pour les objets, "
                "c'est hubspot_object qui la porte)")
        wanted = _names(properties, "properties", op)

        # `properties=[]` demande ZÉRO colonne. Le laisser passer prendrait le
        # chemin enrichi : un `get_list` de plus, puis un batch read dont le
        # corps omet `properties` — auquel HubSpot répond sa projection PAR
        # DÉFAUT. L'appelant paierait trois appels pour des colonnes que
        # personne n'a demandées. Les deux intentions possibles ont déjà chacune
        # leur écriture (omettre l'argument = les ids seuls ; le remplir = des
        # colonnes) ; la troisième se refuse, elle ne se devine pas.
        if wanted is not None and not wanted:
            raise _bad(
                f"op='{op}' attend properties = liste NON VIDE de noms internes "
                "de propriétés ; properties=[] ne demande aucune colonne — omets "
                "l'argument pour n'avoir que les ids d'enregistrements")

        if op == "search":
            return c.search_lists(
                query=query,
                object_type_id=(_object_type_id(object_type, op)
                                if object_type else None))

        if op == "get":
            if list_id:
                return c.get_list(list_id, include_filters=include_filters)
            if name and object_type:
                return c.get_list_by_name(
                    _object_type_id(object_type, op), name,
                    include_filters=include_filters)
            raise _bad("op='get' requiert list_id, ou name + object_type")

        if op == "create":
            if processing_type != "MANUAL" and filter_branch is None:
                raise _bad(
                    f"processing_type='{processing_type}' requiert filter_branch "
                    "(une liste sans critères n'aurait aucun membre)")
            return c.create_list(
                _need(name, "name", op),
                _object_type_id(object_type, op),
                processing_type=processing_type,
                filter_branch=filter_branch)

        if op == "update":
            lid = _need(list_id, "list_id", op)
            if name is None and filter_branch is None:
                raise _bad("op='update' requiert name et/ou filter_branch")
            out: dict = {}
            if name is not None:
                out["renamed"] = c.update_list_name(lid, name)
            if filter_branch is not None:
                out["filters"] = c.update_list_filters(lid, filter_branch)
            return out

        if op == "delete":
            lid = _need(list_id, "list_id", op)
            if dry_run:
                return {"dry_run": True, "would": "delete", "list_id": lid,
                        "current": c.get_list(lid),
                        "note": "restaurable 90 jours via op='restore'"}
            return c.delete_list(lid)

        if op == "restore":
            return c.restore_list(_need(list_id, "list_id", op))

        if op == "members":
            lid = _need(list_id, "list_id", op)
            page = c.get_list_memberships(lid, limit=limit, after=after)
            if wanted is None:
                # Chemin historique INTACT : un appel, sa réponse rendue telle
                # quelle — pas ré-emballée, pas augmentée d'une clé.
                return page
            membres = (page or {}).get("results") or []
            otype = _batch_object_type(c, lid, object_type, op)
            ids = [str(m.get("recordId")) for m in membres
                   if m.get("recordId") is not None]
            # Le découpage à 100 est celui de HubSpot, donc celui du CLIENT :
            # un second découpeur ici serait un miroir que rien ne relie, et
            # qui dériverait en silence. Un batch read vide est un 400 chez
            # HubSpot — une page sans membre n'a rien à lire.
            #
            # `batch_read_objects` rend une ENVELOPPE, pas une liste :
            # `{"results": [...], "missing_ids": [...]}`. On l'ouvre, et on SERT
            # `missing_ids` — c'est le verdict du client sur les ids que HubSpot
            # n'a pas rendus, et le re-dériver ici en jetant le sien ferait de
            # deux calculs un seul chiffre, sans jamais pouvoir les confronter.
            lecture = (c.batch_read_objects(otype, ids, properties=wanted)
                       if ids else {"results": [], "missing_ids": []})
            records, absents = _batch_read_envelope(lecture)
            out = dict(page or {})  # `paging` et `total` survivent verbatim
            out["results"] = _rows_from_memberships(membres, records, wanted)
            out["object_type"] = otype  # provenance : le type réellement lu
            out.update(_missing_report(out["results"], absents))
            return out

        if op == "add_members":
            lid = _need(list_id, "list_id", op)
            ids = _ids(record_ids, "record_ids", op)
            _writable_list(c, lid, op)
            if remove_record_ids:
                return c.add_and_remove_list_memberships(
                    lid, record_ids_to_add=ids,
                    record_ids_to_remove=_ids(
                        remove_record_ids, "remove_record_ids", op))
            return c.add_list_memberships(lid, ids)

        if op == "remove_members":
            lid = _need(list_id, "list_id", op)
            ids = _ids(record_ids, "record_ids", op)
            info = _writable_list(c, lid, op)
            if dry_run:
                return {"dry_run": True, "would": "remove_members",
                        "list_id": lid, "record_ids": ids, "current": info}
            return c.remove_list_memberships(lid, ids)

        if op == "clear_members":
            lid = _need(list_id, "list_id", op)
            info = _writable_list(c, lid, op)
            if dry_run:
                return {"dry_run": True, "would": "clear_members",
                        "list_id": lid, "current": info,
                        "note": "retire TOUS les membres ; la liste survit"}
            return c.delete_all_list_memberships(lid)

        if op == "copy_from":
            lid = _need(list_id, "list_id", op)
            src = _need(source_list_id, "source_list_id", op)
            _writable_list(c, lid, op)
            return c.add_memberships_from_list(lid, src)

        if op == "record_lists":
            return c.get_record_memberships(
                _object_type_id(object_type, op),
                _need(record_id, "record_id", op))

        raise _bad("op doit être 'search', 'get', 'create', 'update', 'delete', "
                   "'restore', 'members', 'add_members', 'remove_members', "
                   "'clear_members', 'copy_from' ou 'record_lists'")

    @mcp.tool()
    def hubspot_property(
        op: Literal["list", "get", "create", "update", "delete", "groups"] = "list",
        object_type: Optional[str] = None,
        property_name: Optional[str] = None,
        definition: Optional[dict] = None,
        archived: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """HubSpot properties — the field schema of a CRM object type.

        Read this BEFORE writing anything. HubSpot's internal property names are
        not the labels shown in the UI (`dealstage`, not "Deal Stage"), and an
        enumeration property only accepts its declared `options[].value` — so a
        create/update written from the label is a guess that fails or, worse,
        silently writes nothing. List criteria (`hubspot_list`'s `filter_branch`)
        reference the same internal names.

        ⚠️ One enumeration is NOT self-describing here: `dealstage` comes back
        with an EMPTY `options` list, because deal stages belong to a pipeline,
        not to the property. Reading this tool is not enough to write a deal
        stage — that needs the Pipelines API, which this connector does not
        expose yet.

        Ops:
        - **"list"** (default) : every property of `object_type`, with its type,
          fieldType and enumeration options.
        - **"get"** : one property, by internal `property_name`.
        - **"create"** : create a property. Requires `definition` — at minimum
          {"name", "label", "type", "fieldType", "groupName"}, plus "options"
          ([{"label", "value"}]) for an enumeration.
        - **"update"** : PATCH a property (e.g. add options). Requires
          `property_name` + `definition`.
        - **"delete"** : archive a property. Requires `property_name`. Supports
          `dry_run`.
        - **"groups"** : the property groups (the tabs of a record page).

        Args:
            op: list | get | create | update | delete | groups.
            object_type: contacts | companies | deals | tickets (or a custom
                object's name). Required for every op.
            property_name: the INTERNAL name — required for get, update, delete.
            definition: op="create"/"update" — the property definition dict.
            archived: op="list"/"get" — return archived properties instead.
            dry_run: op="delete" — report the property that would be archived
                without archiving it.
        """
        c = _client()

        if op == "list":
            return c.list_properties(
                _need(object_type, "object_type", op), archived=archived)

        if op == "get":
            return c.get_property(
                _need(object_type, "object_type", op),
                _need(property_name, "property_name", op), archived=archived)

        if op == "create":
            return c.create_property(
                _need(object_type, "object_type", op),
                _payload(definition, "definition", op))

        if op == "update":
            return c.update_property(
                _need(object_type, "object_type", op),
                _need(property_name, "property_name", op),
                _payload(definition, "definition", op))

        if op == "delete":
            otype = _need(object_type, "object_type", op)
            pname = _need(property_name, "property_name", op)
            if dry_run:
                return {"dry_run": True, "would": "delete", "object_type": otype,
                        "property_name": pname,
                        "current": c.get_property(otype, pname)}
            return c.delete_property(otype, pname)

        if op == "groups":
            return c.list_property_groups(_need(object_type, "object_type", op))

        raise _bad("op doit être 'list', 'get', 'create', 'update', 'delete' "
                   "ou 'groups'")

    @mcp.tool()
    def hubspot_owners() -> dict:
        """List HubSpot owners (users) — to assign records by ownerId."""
        return _client().list_owners()
