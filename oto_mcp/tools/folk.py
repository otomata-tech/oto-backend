"""Folk CRM — groups, people, companies, deals, notes, interactions, tasks, webhooks.

Wrappe `oto.tools.folk.FolkClient` (API publique https://developer.folk.app).
Clé résolue par appel via `access.resolve_api_key("folk")` — provider byo-only
(user key posée sur /account, ou credential partagé de l'org active). Pas de
clé plateforme.

**Surface consolidée (ADR 0047 §Amendement, appliqué au connecteur folk)** : un
tool par OBJET métier, le verbe en paramètre `op` — 17 tools → 4. Ce qui a été
fusionné, et ce qui ne l'a PAS été (le critère est l'homogénéité des paramètres,
pas le comptage) :

- **`folk_record`** (search/get/create/update/delete/add_to_group) — les six
  verbes partagent le MÊME jeu de paramètres (`entity`, `group_id`,
  `object_type`, `id`/`ids`, `dry_run`) ; seul change le porteur de données
  (`filters` en lecture, `item`/`items` en création, `fields` en mise à jour).
  `entity` y joue le rôle que `module` joue chez Zoho : person | company | deal |
  note | interaction | task | reminder. Les ex-`folk_list_deals` /
  `folk_list_notes` / `folk_list_reminders` / `folk_get_reminder` y entrent SANS
  ajouter un paramètre : ce sont `op="search"` / `op="get"` sur une autre
  `entity`. `mark_done`/`mark_todo` sont les deux seules ops à ne valoir que
  pour UNE entité (la tâche) : chez Folk la complétion est un endpoint à part
  (`POST /tasks/{id}/mark-as-done`), refusé dans un PATCH — la replier dans
  `op="update"` aurait menti sur ce que fait l'appel.
- **`folk_group`** (list/create/update/custom_fields/get_custom_field/
  create_custom_field/update_custom_field/members/add_member/remove_member/
  update_member) — reste à part : ni `id`/`ids` (jamais bulk — un workspace a
  peu de groupes), ni `entity` (`entity_type` qualifie un schéma de champs
  custom, il ne désigne pas un objet à écrire). Les ops `*_member` y sont
  entrées par le même critère d'homogénéité (ancrées sur `group_id`, comme les
  sept ops précédentes) plutôt qu'un tool `folk_group_member` séparé dont le
  seul paramètre obligatoire aurait été... `group_id`. Folk n'a **pas
  d'endpoint delete** pour un groupe ni pour un champ custom (vérifié contre la
  doc — seuls list/create/update existent) : on ne peut retirer ni l'un ni
  l'autre via l'API, seulement depuis l'app Folk — un **membre**, en revanche,
  se retire bien via l'API (`op="remove_member"`).
- **`folk_user`** (list/get) — reste à part : un membre du workspace n'est pas un
  record CRM (pas d'`entity`, pas de `group_id`/`object_type`, pas d'écriture).
  Son paramètre `user_id` réapparaît sur `folk_group` (ops `*_member`) — même
  espace d'ids (un membre de groupe EST un user du workspace), pas une
  coïncidence de nommage.
- **`folk_webhook`** (list/create/update) — reste à part : ressource GLOBALE du
  workspace (ni `entity`, ni `group_id`/`object_type`, ni mode bulk — un
  workspace en a peu), avec son propre vocabulaire d'événements validé à
  l'entrée.

Surface : lecture/écriture **par entité** (`op="search"`/`"get"` prennent
`entity` = person|company|deal[|note|reminder]). `op="create"`/`"update"`/
`"delete"`/`"add_to_group"` couvrent aussi note/reminder (et interaction pour
create), et sont **solo OU bulk selon le param passé** : un singulier
(`item`/`id`) pour UN record → résultat direct ; un pluriel (`items`/`ids`, ≤50)
pour plusieurs → reçu allégé (compte + erreurs par item, jamais N corps de
réponse complets). Folk n'a d'endpoint batch nulle part — le mode bulk boucle sur
les méthodes single-record, en PARALLÈLE et à cadence plafonnée (`_bulk_run`),
pas en séquence avec une pause fixe (la latence réseau par appel dominait le
temps total, pas la cadence Folk).

⚠️ **Deux vocabulaires de champs différents cohabitent** : `op="create"` prend
des clés Python snake_case (`first_name`, `company_id`...) ; `op="update"` prend
les noms de champs bruts de l'API Folk en camelCase (`jobTitle`,
`customFieldValues`...). Ne pas transposer l'un vers l'autre — voir le docstring
de `folk_record`. Deux mots changent AUSSI de sens entre create et update, et
sont refusés explicitement plutôt que renvoyés en 422 opaque : `type` devient
`activityType` au PATCH d'une interaction, et `completedAt` (écrivable à la
création d'une tâche) n'est pas patchable — c'est `op="mark_done"`.

**Interactions : lecture, pas seulement écriture.** Le connecteur n'a longtemps
exposé que `create_interaction`, d'où la croyance — écrite noir sur blanc dans
la doc de ce connecteur — qu'on ne pouvait pas RELIRE ce qui s'était dit avec
un contact. C'était vrai du connecteur, pas de Folk : `GET /interactions/past`,
`/upcoming`, `/{id}` (+ PATCH et DELETE) existent, en open beta. Ils sont
maintenant branchés sur `op="search"/"get"/"update"/"delete"`. Une interaction
n'est PAS adressable seule : `entity.id` (la personne/société porteuse) est
obligatoire en query sur listing/get/delete — d'où le paramètre `entity_id`.

**Rappels → tâches.** Folk a déprécié `/reminders` le 2026-08-13 (retrait
annoncé pour février 2027) au profit de `/tasks`, qui fait strictement plus :
`description` markdown, filtres réels (échéance, assigné, complété ou non), et
un suivi de complétion que les rappels n'ont jamais eu. `entity="reminder"`
continue de marcher — le déprécié n'est pas le cassé — mais rien de nouveau ne
devrait s'y brancher. Ce qui N'EST PAS documenté par Folk et reste à vérifier :
si les rappels déjà posés remontent ou non dans `list_tasks`.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from oto.tools.common.errors import UpstreamHTTPError

from .. import access


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable qui NOMME l'op et
    l'argument manquant, jamais un fallback silencieux (les ops d'écriture de ce
    module touchent des données réelles : deviner à la place de l'appelant y
    coûte un record)."""
    if value is None:
        raise _bad(f"op='{op}' requiert {name}")
    return value


_CUSTOM_FIELD_RESERVED_KEYS = {"group_id", "entity_type", "custom_field_name"}


def _reject_reserved_keys(d: dict, param_name: str, op: str) -> None:
    """`create_group_custom_field`/`update_group_custom_field` splattent le dict
    de l'appelant (**field / **fields) sur des paramètres NOMMÉS (group_id,
    entity_type, custom_field_name) — si le dict porte l'une de ces clés,
    `TypeError: got multiple values for keyword argument` remonte en erreur
    opaque au lieu d'un refus actionnable. Même famille de collision que
    `_create_one` (folk_record) : un champ métier mangé par un paramètre
    homonyme."""
    collide = _CUSTOM_FIELD_RESERVED_KEYS & set(d or {})
    if collide:
        raise _bad(
            f"op='{op}' : {param_name} ne doit pas porter {sorted(collide)} — "
            "ce sont des paramètres du tool (group_id/entity_type/"
            "custom_field_name), pas des champs de l'API custom field.")


_AVAILABLE_ENTITY_TYPES_RE = re.compile(r"Available entity types are:\s*(.+)")
_FIXED_ENTITY_TYPES = {"person", "company"}


def _resolve_deal_object_type(c, group_id: str) -> str:
    """`entity="deal"`'s `object_type` a longtemps défaulté à `"deals"` — mais
    l'objet deal est un OBJET CUSTOM que chaque client Folk nomme lui-même
    ("Deals" n'est que le nom choisi PAR CE workspace ; confirmé en live, un
    autre workspace peut l'appeler autrement, ou décliner "deals" en
    majuscule). Sonder plutôt que deviner : tenter "deals" (l'historique), et
    si Folk 404, son propre message énumère les entity_type RÉELS du groupe
    (`"Available entity types are: ..."`) — on y prend celui qui n'est ni
    "person" ni "company". Ambigu (plusieurs objets custom, ex. Deals/Events/
    Projects) ou aucun candidat : erreur actionnable plutôt qu'une supposition
    qui écrirait au mauvais endroit.
    """
    try:
        c.get_group_custom_fields(group_id, entity_type="deals")
        return "deals"
    except UpstreamHTTPError as e:
        if e.status_code != 404:
            raise
        message = (e.body or {}).get("error", {}).get("message", "") \
            if isinstance(e.body, dict) else ""
        match = _AVAILABLE_ENTITY_TYPES_RE.search(message)
        if not match:
            raise  # 404 d'une autre nature (ex. group_id introuvable) — ne pas deviner dessus
        available = re.findall(r'"([^"]+)"', match.group(1))
        candidates = [t for t in available if t not in _FIXED_ENTITY_TYPES]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise _bad(
                f"group_id {group_id!r} n'a pas d'objet deal — objets disponibles : "
                f"{sorted(available)}. Passer `object_type` explicitement si l'un "
                "d'eux convient (voir folk_group(op='custom_fields') pour le détail).")
        raise _bad(
            f"group_id {group_id!r} a plusieurs objets custom {sorted(candidates)} — "
            "impossible de deviner lequel désigne les deals. Passer `object_type` "
            "explicitement.")


def _merge_group_ids(current_groups, add, remove) -> list[dict]:
    """Fusionne la liste de groupes d'un record Folk et renvoie la liste COMPLÈTE
    au format API (`[{"id": ...}]`).

    L'API Folk est en *replace-all* sur les champs-listes (un PATCH `groups`
    écrase la liste entière) : pour ajouter/retirer un groupe sans perdre les
    autres, il faut relire les groupes actuels et renvoyer l'union résultante.
    Préserve l'ordre et déduplique.
    """
    remove_set = set(remove or [])
    result: list[str] = []
    for g in (current_groups or []):
        gid = g.get("id") if isinstance(g, dict) else g
        if gid and gid not in remove_set and gid not in result:
            result.append(gid)
    for gid in (add or []):
        if gid not in remove_set and gid not in result:
            result.append(gid)
    return [{"id": gid} for gid in result]


def _merge_custom_fields(current_cfv, patch: dict) -> dict:
    """Fusionne `customFieldValues` et renvoie l'objet COMPLET attendu par l'API.

    Même faute que `groups` juste au-dessus, et bien plus coûteuse : l'API Folk est en
    *replace-all* sur cet objet aussi. Passer un seul champ personnalisé effaçait tous
    les autres de ce groupe sur la fiche — silencieusement, avec `succeeded: 1` en
    retour. Mesuré le 04/09/2026 : **quatre champs perdus en un appel**, dont une
    consigne opérationnelle (oto-backend, signal 714).

    ⚠️ La documentation de l'outil DIT que `groups` est un remplacement et offre
    `add_to_groups`/`remove_from_groups` pour l'éviter. C'est cette précaution visible
    sur le champ voisin qui trompe : elle fait conclure que la fusion est le défaut
    ailleurs. Un remède qui ne traiterait que `groups` laisserait donc intact le champ
    où le même défaut coûte le plus.

    **Ce qui est fourni gagne, ce qui est absent survit.** La granularité est le CHAMP,
    pas le groupe : les autres groupes de la fiche sont conservés tels quels, et dans le
    groupe visé les clés non citées restent en place.

    ⚠️ **Une valeur explicitement fournie est écrite telle quelle, `None` et `""`
    compris** — c'est ainsi qu'on VIDE un champ, et il faut que ça reste possible :
    l'incident fondateur s'est réparé par un second appel qui remettait à vide. Fusionner
    « sauf les valeurs vides » enlèverait le seul geste d'effacement disponible.
    """
    out: dict = {}
    for gid, champs in (current_cfv or {}).items():
        out[str(gid)] = dict(champs) if isinstance(champs, dict) else champs
    for gid, champs in (patch or {}).items():
        gid = str(gid)
        ancien = out.get(gid)
        if isinstance(ancien, dict) and isinstance(champs, dict):
            ancien.update(champs)
        else:
            # Groupe absent de la fiche, ou forme inattendue d'un côté : on écrit ce
            # que l'appelant a fourni. Rien n'est écrasé qu'on aurait pu préserver.
            out[gid] = dict(champs) if isinstance(champs, dict) else champs
    return out


# --- dispatch par entité, partagé entre modes singulier et bulk -------------
#
# `_create_one`/`_update_one`/`_delete_one` portent la logique de l'op sur UN
# record : on l'extrait pour que le mode bulk l'appelle item-par-item sans
# dupliquer/diverger de la validation. Tous les trois acceptent `dry_run`
# (convention oto — cf. `email_send`, LinkedIn `send_message`/`connect`) : la
# validation tourne normalement, seul l'appel mutant final est sauté, remplacé
# par un aperçu.

# Axes de dispatch de `folk_record`, DÉCLARÉS au schéma (`Literal` → `enum` JSON).
# Depuis la consolidation, le verbe n'est plus dans le NOM du tool : sans enum, les
# valeurs admises n'existent que dans la prose de la docstring, et rien ne contraint
# le client. `_Entity` borne l'UNION des entités (= tout ce qu'au moins une op
# accepte) ; le sous-ensemble admis PAR op reste gardé par les tuples ci-dessous.
_Entity = Literal["person", "company", "deal", "note", "interaction", "task",
                  "reminder"]
_RecordOp = Literal["search", "get", "create", "update", "delete",
                    "add_to_group", "mark_done", "mark_todo"]

_SEARCH_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_GET_ENTITIES = ("person", "company", "deal", "interaction", "task", "reminder")
_CREATE_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_UPDATE_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_DELETE_ENTITIES = ("person", "company", "deal", "note", "interaction", "task",
                    "reminder")
_GROUP_ENTITIES = ("person", "company")
# `mark_done`/`mark_todo` n'existent que sur la tâche : chez Folk la complétion
# est un appel À PART (`POST /tasks/{id}/mark-as-done`), jamais un PATCH — une
# tâche ne se termine pas toute seule, contrairement à un rappel qui se marque
# « déclenché » sur son propre calendrier.
_MARK_ENTITIES = ("task",)

# Entités dont l'id n'est adressable QUE via l'entité parente : Folk exige
# `entity.id` sur les DEUX endpoints de listing, sur le get et le delete (en
# query), et sur le PATCH (dans le corps) — il n'existe pas de « lire
# l'interaction lit_… » tout court. Le PATCH a été vérifié en live le
# 2026-08-27 : la spec OpenAPI ne marque pas `entity` requis, Folk répond
# pourtant 422 `path: ['entity'], Required` sans lui.
_ENTITY_ID_REQUIRED = ("interaction",)

# Champs acceptés par `op="create"` par entité — miroir des paramètres nommés
# des méthodes `FolkClient.create_*` (snake_case Python, PAS les noms de
# champs API Folk en camelCase utilisés par `op="update"`/`fields`). Codé en
# dur plutôt qu'introspecté via `inspect.signature` : `create_person`/
# `create_company` acceptent `**kwargs` côté client, donc sans cette
# allow-list explicite un champ mal orthographié/mal casé (ex. `firstName` au
# lieu de `first_name`) serait avalé SILENCIEUSEMENT dans le payload envoyé à
# Folk sous le mauvais nom, plutôt que de lever une erreur. Une liste codée en
# dur reste aussi testable contre un `FolkClient` mocké (l'introspection de
# signature ne fonctionne pas sur un Mock sans `autospec`).
_CREATE_FIELDS = {
    "person": {"first_name", "last_name", "emails", "phones", "job_title",
               "company_name", "company_id", "group_ids", "urls", "description"},
    "company": {"name", "emails", "industry"},
    "deal": {"name", "people_ids", "company_ids", "custom_fields"},
    "note": {"entity_id", "content", "visibility"},
    "interaction": {"entity_id", "type", "title", "content", "date_time"},
    "task": {"entity_id", "title", "due_at", "due_time", "description",
             "recurrence_frequency", "assigned_users", "is_public"},
    "reminder": {"entity_id", "name", "recurrence_rule", "visibility"},
}

# Champs qu'un `op="update"` doit REFUSER, avec le chemin à prendre à la place.
# Deux pièges hérités d'asymétries de l'API Folk elle-même — sans ce garde,
# chacun rend un 422 opaque là où l'appelant a juste pris le mauvais mot :
#   - `type` est le nom du champ à la CRÉATION d'une interaction, mais le PATCH
#     l'appelle `activityType` (même valeur, autre clé) ;
#   - `completedAt` s'écrit à la création d'une tâche, mais le PATCH le refuse
#     (`additionalProperties: false`) : compléter, c'est `op="mark_done"`.
_UPDATE_FORBIDDEN_FIELDS = {
    "interaction": {
        "type": "le PATCH d'une interaction nomme ce champ `activityType` "
                "(c'est `type` seulement à la création).",
    },
    "task": {
        "completedAt": "la complétion d'une tâche n'est pas un PATCH — "
                       "utiliser op='mark_done' (ou op='mark_todo' pour la "
                       "rouvrir).",
    },
}

# Filtres acceptés par `op="search"` sur note/reminder : Folk n'expose qu'un
# filtre par entité parente (`list_notes(entity_id=…)`). Contrairement à
# `list_people(**filters)`, ces méthodes ont une signature FERMÉE — un filtre
# inconnu lèverait un `TypeError` rendu en « erreur interne », là où l'appelant
# doit lire quel filtre existe.
_SUBRECORD_FILTERS = {"entity_id"}


def _reject_forbidden_update_fields(entity: str, fields: dict) -> None:
    """Refuse, en le NOMMANT, un champ qui existe ailleurs sur la même entité
    mais pas dans son PATCH. Sans ça l'appelant reçoit un 422 Folk opaque
    (`unrecognized_keys`) sur un mot qu'il a lu dans ce même docstring — au
    rayon création."""
    for name, why in _UPDATE_FORBIDDEN_FIELDS.get(entity, {}).items():
        if name in fields:
            raise _bad(f"op='update' entity='{entity}' : champ `{name}` "
                       f"refusé — {why}")


def _get_one(c, entity: str, id: str, group_id: Optional[str] = None,
             object_type: str = "deals", entity_id: Optional[str] = None):
    """Récupère l'état courant d'un record, pour diff/preview `dry_run`.

    Renvoie `None` pour `note` : Folk n'a PAS d'endpoint get-par-id pour les
    notes (`client.py` n'expose que list/create/update/delete) — un gap
    permanent de l'API, pas un raccourci d'implémentation. Les previews
    update/delete d'une note dégradent en conséquence (pas de diff possible).

    Le cas « interaction sans `entity_id` » ne se produit plus : les trois ops
    qui appellent `_get_one` sur une interaction (get, update, delete) l'exigent
    toutes en amont. Le garde reste par sûreté."""
    if entity == "person":
        return c.get_person(id)
    if entity == "company":
        return c.get_company(id)
    if entity == "deal":
        if not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        return c.get_deal(group_id, id, object_type=object_type)
    if entity == "interaction":
        return c.get_interaction(id, entity_id) if entity_id else None
    if entity == "task":
        return c.get_task(id)
    if entity == "reminder":
        return c.get_reminder(id)
    return None


def _create_one(c, entity: str, fields: Optional[dict] = None,
                 group_id: Optional[str] = None,
                 object_type: str = "deals", dry_run: bool = False):
    """Crée UN record. `fields` = l'item de l'appelant, passé comme DICT.

    Surtout pas `**fields` : les clés de l'item viennent de l'agent, et l'une
    d'elles peut porter le nom d'un paramètre de cette fonction — `folk_record
    (op='create', entity='person', item={... 'group_id': 'grp_…'})` levait alors
    un `TypeError: got multiple values for keyword argument 'group_id'`, rendu à
    l'appelant en « erreur interne du serveur » là où il attendait le refus
    actionnable « champ inconnu pour entity='person' » que la validation juste
    en dessous sait produire (signal #353). Même famille que la collision des
    jetons de contexte : un argument métier mangé par un paramètre homonyme.
    Passer le dict ferme la collision par construction, pour toute clé future.
    """
    fields = dict(fields or {})
    if entity == "deal" and not group_id:
        raise _bad("group_id requis pour entity='deal'.")
    unknown = set(fields) - _CREATE_FIELDS.get(entity, set())
    if unknown:
        raise _bad(
            f"champ(s) inconnu(s) pour entity='{entity}' : {sorted(unknown)}. "
            f"Champs acceptés : {sorted(_CREATE_FIELDS.get(entity, set()))}. "
            f"Rappel : op='create' utilise des clés snake_case Python "
            f"(first_name, company_id...) — PAS les noms de champs API Folk "
            f"en camelCase (jobTitle, customFieldValues...) utilisés par op='update'.")
    if dry_run:
        preview = {"would_create": fields}
        if entity == "deal":
            preview.update(group_id=group_id, object_type=object_type)
        return preview
    if entity == "person":
        return c.create_person(**fields)
    if entity == "company":
        return c.create_company(**fields)
    if entity == "deal":
        return c.create_deal(group_id, object_type=object_type, **fields)
    if entity == "note":
        return c.create_note(**fields)
    if entity == "interaction":
        return c.create_interaction(**fields)
    if entity == "task":
        return c.create_task(**fields)
    if entity == "reminder":
        return c.create_reminder(**fields)
    raise _bad(f"entity doit être l'un de {_CREATE_ENTITIES}.")


def _update_one(c, entity: str, id: str, fields: Optional[dict] = None,
                 group_id: Optional[str] = None, object_type: str = "deals",
                 add_to_groups: Optional[list[str]] = None,
                 remove_from_groups: Optional[list[str]] = None,
                 dry_run: bool = False, entity_id: Optional[str] = None):
    fields = dict(fields or {})
    _reject_forbidden_update_fields(entity, fields)
    current = None
    # `customFieldValues` rejoint la liste des champs qui EXIGENT l'état actuel : sans
    # lui, un patch partiel est une destruction (cf. `_merge_custom_fields`).
    besoin_courant = bool(add_to_groups or remove_from_groups or dry_run
                          or isinstance(fields.get("customFieldValues"), dict))
    if besoin_courant:
        current = _get_one(c, entity, id, group_id=group_id,
                           object_type=object_type, entity_id=entity_id)
    if isinstance(fields.get("customFieldValues"), dict):
        # ⚠️ REFUSER plutôt qu'écrire à l'aveugle. Sans l'état actuel on ne peut pas
        # fusionner, et envoyer le patch tel quel écraserait les champs qu'on n'a pas
        # pu lire — exactement le dégât que ce lot corrige. Un refus nommé laisse
        # l'appelant choisir ; un succès muet ne laisse rien.
        if current is None:
            raise _bad(
                "Impossible de relire la fiche pour fusionner `customFieldValues` : "
                "l'API Folk REMPLACE cet objet, donc écrire sans l'état actuel "
                "effacerait les champs personnalisés non fournis. Réessaie, ou passe "
                "l'objet complet après un op='get'.")
        fields["customFieldValues"] = _merge_custom_fields(
            current.get("customFieldValues"), fields["customFieldValues"])
    if add_to_groups or remove_from_groups:
        if entity not in _GROUP_ENTITIES:
            raise _bad("add_to_groups/remove_from_groups ne valent que pour "
                       "entity='person' ou 'company'.")
        if "groups" in fields:
            raise _bad("Ne pas passer 'groups' dans fields en même temps que "
                       "add_to_groups/remove_from_groups.")
        fields["groups"] = _merge_group_ids(
            (current or {}).get("groups"), add_to_groups, remove_from_groups)
    if not fields:
        raise _bad("Rien à mettre à jour : fournir `fields` et/ou "
                   "add_to_groups/remove_from_groups.")
    if dry_run:
        if current is not None:
            return {"id": id, "changes": {k: {"from": current.get(k), "to": v}
                                          for k, v in fields.items()}}
        return {"id": id, "fields": fields, "current_available": False}
    if entity == "person":
        return c.update_person(id, **fields)
    if entity == "company":
        return c.update_company(id, **fields)
    if entity == "deal":
        if not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        return c.update_deal(group_id, id, object_type=object_type, **fields)
    if entity == "note":
        return c.update_note(id, **fields)
    if entity == "interaction":
        return c.update_interaction(
            id, _need(entity_id, "entity_id", "update (entity='interaction')"),
            **fields)
    if entity == "task":
        return c.update_task(id, **fields)
    if entity == "reminder":
        return c.update_reminder(id, **fields)
    raise _bad(f"entity doit être l'un de {_UPDATE_ENTITIES}.")


def _delete_one(c, entity: str, id: str, group_id: Optional[str] = None,
                 object_type: str = "deals", dry_run: bool = False,
                 entity_id: Optional[str] = None):
    if dry_run:
        current = _get_one(c, entity, id, group_id=group_id,
                           object_type=object_type, entity_id=entity_id)
        if current is not None:
            return {"id": id, "would_delete": current}
        return {"id": id, "would_delete": None, "current_available": False}
    if entity == "person":
        return c.delete_person(id)
    if entity == "company":
        return c.delete_company(id)
    if entity == "deal":
        if not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        return c.delete_deal(group_id, id, object_type=object_type)
    if entity == "note":
        return c.delete_note(id)
    if entity == "interaction":
        return c.delete_interaction(id, _need(entity_id, "entity_id", "delete"))
    if entity == "task":
        return c.delete_task(id)
    if entity == "reminder":
        return c.delete_reminder(id)
    raise _bad(f"entity doit être l'un de {_DELETE_ENTITIES}.")


def _mark_one(c, id: str, done: bool, completed_at: Optional[str] = None,
              dry_run: bool = False):
    """`op="mark_done"` / `op="mark_todo"` sur UNE tâche.

    L'aperçu relit la tâche : il fait voir ce qui va être clos (titre,
    échéance) et son `completedAt` actuel — refermer une tâche déjà close, ou
    en rouvrir une jamais complétée, est un no-op silencieux côté Folk."""
    if dry_run:
        current = c.get_task(id)
        return {"id": id,
                "would_mark": "done" if done else "todo",
                "current": current}
    if done:
        return c.mark_task_done(id, completed_at=completed_at)
    return c.mark_task_todo(id)


# 50 reste une limite d'ergonomie d'appel (pas de constat précis dérrière),
# indépendante de la cadence ci-dessous.
_BULK_MAX_ITEMS = 50

# Folk documente 600 req/min (10 req/s) par clé. Le goulot d'un lot n'est PAS
# cette cadence — c'est la latence réseau par appel, non recouverte tant que
# les appels étaient séquentiels (un délai de courtoisie fixe entre appels
# n'accélère rien, il ajoute juste une pause après une attente déjà payée).
# `_BULK_CONCURRENCY` appels en vol en parallèle recouvrent cette latence ;
# `_RateLimiter` plafonne la cadence D'ENVOI combinée (tous workers confondus)
# à ~8 req/s, sous les 10 req/s documentés avec marge pour le trafic
# concurrent d'autres appels sur la même clé. `_request` gère déjà les 429
# (retry sur Retry-After) : le régulateur vise à rester sous la limite en
# usage normal, pas à s'y substituer.
_BULK_CONCURRENCY = 6
_BULK_MIN_INTERVAL_S = 0.125  # ~8 req/s


class _RateLimiter:
    """Espace les DISPATCHES d'appel à un intervalle minimum PARTAGÉ entre tous
    les workers — un délai par-worker ne suffirait pas : N workers respectant
    chacun leur propre délai peuvent quand même émettre N fois plus vite que
    prévu au global."""

    def __init__(self, min_interval_s: float):
        self._min_interval = min_interval_s
        self._lock = Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_at)
            self._next_at = start_at + self._min_interval
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)


def _bulk_fatal(exc: Exception) -> bool:
    """Erreurs d'auth/connexion : on abandonne tout le lot (répéter la même
    erreur N fois ne sert à rien). Tout le reste (un enregistrement rejeté,
    422 Folk…) reste une erreur PAR ITEM qui n'interrompt pas le lot."""
    from oto.tools.common.errors import UpstreamHTTPError
    import requests
    if isinstance(exc, UpstreamHTTPError):
        return exc.status_code in (401, 403)
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def _bulk_run(items: list, fn) -> list[tuple[int, bool, object]]:
    """Exécute `fn(item)` pour chaque item EN PARALLÈLE (jusqu'à
    `_BULK_CONCURRENCY` appels HTTP en vol, cadence combinée plafonnée par
    `_RateLimiter`) plutôt qu'en séquence avec une pause fixe après chaque
    appel — c'est la latence réseau par appel qui dominait le temps total, pas
    la cadence Folk, et une boucle séquentielle ne pouvait jamais la recouvrir.

    Renvoie une liste de `(index, ok, valeur_ou_message_erreur)` — comme avant
    mais PAS nécessairement dans l'ordre de soumission : chaque appelant ne se
    fie qu'à l'`index` porté par le tuple, jamais à la position dans la liste
    (vérifié aux 4 call-sites). Une erreur FATALE (auth/connexion) annule les
    appels pas encore démarrés et relève l'exception — même contrat qu'avant
    (le lot entier est perdu, pas de reçu partiel), simplement détecté plus
    tôt grâce au parallélisme."""
    if len(items) > _BULK_MAX_ITEMS:
        raise _bad(f"trop d'éléments ({len(items)}) — max {_BULK_MAX_ITEMS} par appel, "
                   f"découper en plusieurs appels.")
    limiter = _RateLimiter(_BULK_MIN_INTERVAL_S)
    results: list[Optional[tuple[int, bool, object]]] = [None] * len(items)

    def _run_one(item):
        limiter.wait()
        return fn(item)

    pool = ThreadPoolExecutor(max_workers=min(_BULK_CONCURRENCY, len(items)))
    futures = {pool.submit(_run_one, item): i for i, item in enumerate(items)}
    try:
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = (i, True, future.result())
            except Exception as e:
                if _bulk_fatal(e):
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                results[i] = (i, False, str(e))
    finally:
        pool.shutdown(wait=True)
    return [r for r in results if r is not None]


def register(mcp: FastMCP) -> None:
    from oto.tools.folk.client import FolkClient, WEBHOOK_EVENT_TYPES

    def _client() -> FolkClient:
        key, _ = access.resolve_api_key("folk")
        # Rédaction des champs sensibles : plus au niveau client — appliquée à la
        # frontière des tools par `FieldRedactionMiddleware` (policy de l'org active).
        return FolkClient(api_key=key)

    def _validate_subscribed_events(events: list) -> None:
        if not events:
            raise _bad("subscribed_events : au moins un événement requis.")
        for e in events:
            event_type = (e or {}).get("eventType")
            if event_type not in WEBHOOK_EVENT_TYPES:
                raise _bad(
                    f"eventType invalide : {event_type!r}. Valeurs valides : "
                    + ", ".join(sorted(WEBHOOK_EVENT_TYPES))
                )

    # --- le record CRM : un tool, le verbe en `op` ---------------------------
    #
    # `op` par défaut = "search", une LECTURE : aucune op d'écriture n'est
    # atteignable sans l'avoir nommée. Les quatre ops mutantes
    # (create/update/delete/add_to_group) prennent une paire de params
    # mutuellement exclusifs : le singulier (un seul record, résultat/preview
    # renvoyé directement) OU le pluriel (jusqu'à 50, reçu bulk). Folk n'a
    # d'endpoint batch nulle part (vérifié sur ce connecteur, le MCP officiel
    # Folk, et un MCP tiers) — le pluriel boucle sur les méthodes
    # single-record, en parallèle à cadence plafonnée (`_bulk_run`) et renvoie
    # un reçu allégé, jamais N corps de réponse complets.

    @mcp.tool()
    def folk_record(
        entity: _Entity,
        op: _RecordOp = "search",
        id: Optional[str] = None,
        ids: Optional[list[str]] = None,
        item: Optional[dict] = None,
        items: Optional[list[dict]] = None,
        fields: Optional[dict] = None,
        filters: Optional[dict] = None,
        max_results: int = 100,
        add_to_groups: Optional[list[str]] = None,
        remove_from_groups: Optional[list[str]] = None,
        group_id: Optional[str] = None,
        object_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        when: Optional[Literal["past", "upcoming", "all"]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Folk CRM records — people (contacts), companies, deals (or any other
        custom object), notes, interactions, tasks, reminders: search, read,
        create, update, delete, add to a group, mark a task done. This is the
        tool for "find/add/update a contact", "look up a company", "list deals",
        "what did we say to X", "what's still open on X" etc. — Folk has no
        separate `folk_company`/`folk_contact`/`folk_deal` tool; `entity` picks
        the noun.

        `entity` scopes every op (person | company | deal | note | interaction |
        task | reminder); `op` picks the verb:

        - **"search"** (default): search records of that entity. Fetches ALL
          matching pages — always pass `filters` on a large workspace. Works on
          every entity, but three of them are addressed by their PARENT record
          rather than by a query: note and reminder take
          `filters={"entity_id": …}`, interaction takes `entity_id` (required)
          + `when`.
        - **"get"**: fetch one record by ID (full record). Every entity except
          note — Folk has NO get-by-id endpoint for notes. `interaction`
          additionally needs `entity_id` (see below).
        - **"create"**: create one (`item`) or several (`items`, ≤50) records.
        - **"update"**: PATCH one (`id`) or several (`items`, ≤50) records —
          only the given fields change.
        - **"delete"**: delete one (`id`) or several (`ids`, ≤50) records.
          Irreversible.
        - **"mark_done"** / **"mark_todo"**: close / reopen one (`id`) or
          several (`ids`, ≤50) **tasks**. `entity="task"` only, and a separate
          op on purpose: Folk refuses `completedAt` in a task PATCH. Nothing in
          Folk ever completes a task on its own — it only moves when something
          calls this.
        - **"add_to_group"**: add one (`id`) or several (`ids`, ≤50) existing
          people/companies to ONE group (`group_id` = the target group). The
          inverse of `op="update"`'s `add_to_groups` (which batches *groups* for
          *one* record) — this batches *records* into *one* group. Reads each
          record's current groups and writes back the union (Folk's `groups`
          field is replace-all on PATCH), so existing group membership is
          preserved. A record already in the group is a no-op success, not an
          error.

        📖 **Reading what actually happened.** `entity="interaction"` is
        readable, not just writable: `op="search"` returns the emails, calendar
        events and manually-logged interactions Folk holds for a person or
        company. (These endpoints are in Folk's **open beta** — the shape may
        still move.) Three things verified live on 2026-08-27, each of which
        changes how you use it:

        - **`op="search"` gives you `content: {subject, snippet}` — NOT the
          body.** The full `body` only comes back from `op="get"` on one
          interaction. So a sweep tells you what was discussed; reading what was
          actually *said* costs one `get` per interaction.
        - **`privacyLevel` withholds the body, not the subject.** On
          `subjectOnly`/`sensitive`/`internal`, `get` still returns subject and
          snippet but `body` is simply absent. The key is BYO, so what comes
          back is what ITS owner may see. A missing `body` is a permission
          outcome, not an empty interaction.
        - **Imported interactions are read-only.** Folk refuses `op="update"`
          and `op="delete"` on anything it pulled in from email, calendar or
          WhatsApp — those belong to their source. Only `interactionType:
          "logged"` records are writable.

        An org-level field-redaction policy, where one is set, can strip fields
        on top of all that.

        ⏳ **Tasks, not reminders — and they are the SAME records.** Folk
        **deprecated** `/reminders` on 2026-08-13 (removal announced for
        February 2027) in favour of `/tasks`. Verified live on 2026-08-27:
        the two are **one store with two views**, not two collections. A
        workspace with 30 reminders has exactly those 30 as tasks, sharing the
        same UUID under a different prefix — `rmd_<uuid>` and `tsk_<uuid>` are
        the same record, and swapping the prefix resolves in both directions.

        So nothing is stranded: a reminder created before the switch is
        readable, filterable and closable as a task today, and `op="mark_done"`
        works on it. Use `entity="task"` for everything new — it does strictly
        more (a markdown `description`, real filters on due date / assignee /
        completion, and completion tracking, which the reminder view has no
        verb for). Field mapping when porting: name→title,
        recurrence_rule→due_at/due_time + recurrence_frequency,
        visibility→is_public.

        The four write ops are **solo OR bulk depending on which param you
        pass**: exactly one of `item`/`items` (create), `id`/`items` (update),
        `id`/`ids` (delete, add_to_group) is required. Solo returns the record
        (or its dry_run preview) directly; bulk returns a receipt (count +
        per-item errors), never N full response bodies.

        ⚠️ **Two different field vocabularies coexist.** `op="create"` field
        names are Python **snake_case** parameter names (`first_name`,
        `company_id`...), forwarded directly to the client — NOT Folk's raw
        camelCase API field vocabulary (`jobTitle`, `customFieldValues`...) that
        `op="update"`'s `fields` uses. An unrecognized create field name raises
        immediately (listing the accepted ones), it is never silently dropped or
        sent under the wrong name. Don't mix the two conventions.

        Per-entity field shape for op="create" (same for `item` and each entry
        of `items`, `*` = required, snake_case — see the warning above):
            person: {first_name*, last_name, emails, phones, job_title,
                company_name, company_id, group_ids, urls, description}
            company: {name*, emails, industry}
            deal: {name*, people_ids, company_ids, custom_fields}
            note: {entity_id*, content*, visibility}
            interaction: {entity_id*, type*, title*, content, date_time}
                — `date_time` is REQUIRED by Folk even though it reads as
                optional here; omitted, it defaults to now rather than failing
                with an opaque 422 (which is what the connector did until
                2026-08-27).
            task: {entity_id*, title*, due_at*, due_time, description,
                recurrence_frequency, assigned_users, is_public}
            reminder: {entity_id*, name*, recurrence_rule*, visibility}
                — DEPRECATED by Folk, use task.

        `task` field notes: `due_at` is a date "YYYY-MM-DD", `due_time` an
        optional "HH:mm"; `description` is markdown; `recurrence_frequency` ∈
        weekday | weekly | biweekly | monthly | quarterly | yearly;
        `assigned_users` takes user IDs **or** emails (a list of either, or of
        {"id"}/{"email"} dicts) — never both in the same call, Folk rejects a
        mixed list; `is_public` false = visible only to the assignees. Omit
        `assigned_users` and the task lands on the API key's owner.

        Returns, per op —
            search: {"entity", "count", "results"} — plus "when" and
                "truncated" for interactions (and "past_count"/
                "upcoming_count" when when="all").
            get: the record.
            create solo: the created record, or {"dry_run": true, "would_create": {...}}.
            create bulk: {"total", "succeeded", "created": [{"index","id"}],
                "failed": [...]}, or dry_run: {"dry_run": true, "total",
                "would_create": [...], "failed": [...]}.
            update/add_to_group solo: the updated record, or {"dry_run": true,
                "id", "changes"|"fields", ...}.
            update/add_to_group bulk: {"total", "succeeded", "failed":
                [{"index","id","error"}]}, or dry_run: {"dry_run": true, "total",
                "would_update"|"would_add": [...], "failed": [...]}.
            delete solo: {} (or {"dry_run": true, "id", "would_delete", ...}).
            delete bulk: {"total", "succeeded", "failed": [{"index","id","error"}]},
                or dry_run: {"dry_run": true, "total", "would_delete": [...],
                "failed": [...]}.
            mark_done/mark_todo solo: the task, or {"dry_run": true, "id",
                "would_mark", "current"}.
            mark_done/mark_todo bulk: {"total", "succeeded", "failed":
                [{"index","id","error"}]}, or dry_run: {"dry_run": true,
                "total", "would_mark": [...], "failed": [...]}.

        Args:
            entity: "person" (a contact), "company", "deal" (or any other
                custom object collection — see `object_type`), "note",
                "interaction", "task" or "reminder" (deprecated — see above) —
                see each op for the ones it accepts (notes have no get-by-id,
                add_to_group is person/company only, mark_done/mark_todo are
                task only).
            op: search (default) | get | create | update | delete |
                add_to_group | mark_done | mark_todo.
            id: the record ID (the deal_id for a deal, tsk_… for a task,
                rmd_… for a reminder) — op="get", and solo mode of
                update/delete/add_to_group/mark_done/mark_todo.
            ids: record IDs — bulk mode of delete/add_to_group/mark_done/
                mark_todo (deal IDs for entity="deal").
            item: op="create" solo — fields for ONE record, see the per-entity
                shape below.
            items: op="create" bulk — fields for MULTIPLE records, same shape as
                `item`, one dict per record. op="update" bulk — one
                `{"id", "fields", "add_to_groups", "remove_from_groups"}` per
                record, same field vocabulary as below.
            fields: op="mark_done" — optionally {"completedAt": "<ISO 8601>"};
                omitted, the task is stamped as completed now. Not accepted by
                op="mark_todo".
                op="update" solo — Folk API field names, camelCase (e.g.
                {"jobTitle": "CTO"}, {"industry": "SaaS"}, ou champs custom d'un
                deal). Optionnel si seuls `add_to_groups`/`remove_from_groups`
                sont fournis.
                **Champs CUSTOM d'une person/company** (ex. Status d'un groupe) :
                les passer SOUS `customFieldValues`, keyés par group_id —
                `{"customFieldValues": {"<group_id>": {"Status": "Follow-up"}}}`.
                Un champ custom passé à plat (`{"Status": …}`) est rejeté (422
                "Unrecognized key"). La structure se découvre via op="search"
                (customFieldValues groupée par group_id).
                ✅ **Patch PARTIEL : les champs que tu ne cites pas sont
                CONSERVÉS.** L'API Folk remplace cet objet en entier ; l'outil
                relit la fiche et fusionne champ par champ avant d'écrire, donc
                envoyer un seul champ n'efface plus les autres. Pour VIDER un
                champ, envoie-le explicitement à `null` ou `""` — une valeur
                fournie est écrite telle quelle. Si la fiche ne peut pas être
                relue, l'appel est REFUSÉ plutôt qu'écrit à l'aveugle.
                ⚠️ Un champ custom peut porter une valeur que tu n'as JAMAIS
                envoyée : folk remplit tout seul ses « AI fields », réglage
                invisible depuis l'API. ⚠️ Ça n'arrive PAS qu'à l'entrée dans un
                groupe — mesuré le 04/09/2026 sur un `op="update"` ordinaire qui
                n'envoyait que deux champs : un troisième est revenu peuplé en
                relecture, `null` au read-back précédent. Une valeur relue n'est
                donc pas une preuve de ce que TU as écrit. Relis la fiche si la
                valeur t'engage, et ne conclus jamais d'un read-back que ton
                écriture a porté.
            filters: op="search" — Field → value, matched with `like` (e.g.
                {"fullName": "Dupont", "emails": "@otomata.tech"} for people,
                {"name": "Otomata"} for companies).
                ⚠️ **Le `like` de Folk est ANCRÉ EN DÉBUT de chaîne : c'est un
                préfixe, pas un « contient ».** Mesuré le 04/09/2026 : chercher
                `{"name": "Cradle"}` rend `count=0` alors qu'une société dont le nom
                CONTIENT ce mot existe dans l'espace. ⚠️ Le coût n'est pas la
                recherche ratée, c'est ce qui vient après : `count=0` se lit « cette
                fiche n'existe pas », et le geste suivant est une CRÉATION — donc un
                doublon de ce qu'on cherchait. **Avant de conclure à l'absence sur un
                nom composé, cherche sur son premier mot, ou vérifie par un autre
                champ (domaine, e-mail).** For another operator, pass
                {field: {op: value}} — op ∈ eq, not_eq, like, not_like, empty,
                not_empty, gt (dates), in / not_in (relations). For `note` and
                `reminder`, Folk only has ONE filter: {"entity_id": "<id>"} (the
                person/company/deal the note or reminder hangs off) — or pass
                `entity_id` directly, same thing.
                For `interaction`, Folk exposes NO filter at all: use
                `entity_id` + `when`.
                For `task`, filters are `{field: {operator: value}}` over a
                CLOSED set — dueAt (eq/not_eq/gt/lt), createdAt (gt/lt),
                completedAt (empty/not_empty/gt/lt), assigneeUserId (in/not_in),
                entity (in/not_in). A bare value means `eq` on dueAt and `in` on
                entity/assigneeUserId; the two others demand an explicit
                operator. There is NO `like` here, unlike people/companies — an
                unknown field or operator is refused, naming what exists.
                Open tasks on someone: `entity_id="per_…"` +
                filters={"completedAt": {"empty": True}}. Overdue and still
                open: filters={"dueAt": {"lt": "<today>"},
                "completedAt": {"empty": True}}.
            max_results: op="search" — truncate the response (default 100).
                `count` reports the REAL total, so a `count` above the number of
                `results` means the list was cut. **`entity="interaction"` is
                the exception**: Folk offers no filter there and serves 30 per
                page, and one active contact can hold hundreds (>360 measured
                on a single record), so the search STOPS at `max_results`
                instead of draining the collection — `count` is what came back
                and `truncated: true` says more exists. Ask for a bigger
                `max_results` to go deeper; there is no way to ask Folk for
                "the newest 10" more cheaply than reading one page of 30.
            add_to_groups: op="update" — rattacher une **person** ou **company**
                À des groupes (`folk_group` pour les IDs), sans toucher ses
                autres groupes — solo mode only.
            remove_from_groups: op="update" — détacher une **person** ou
                **company** DE des groupes, sans toucher ses autres groupes —
                solo mode only.
            group_id: the group concerned by the call, meaning set by op/entity —
                op="search" on `person`/`company`: LIST THE MEMBERS of that group
                (get its id from `folk_group`) — e.g. audit the "Leads" pipeline;
                op="add_to_group": the TARGET group the record(s) join;
                REQUIRED for `entity="deal"` on every other op (the group where
                the deal lives — on create, all record(s) land in this one group,
                Folk deals aren't creatable across groups in a single call). Ne
                PAS le passer pour person/company hors des deux cas ci-dessus.
            entity_id: the PARENT record (person `per_…`, company `com_…`, or
                object `obj_…`) a sub-record hangs off. **Required** for
                entity="interaction" on search/get/update/delete — Folk has no
                "read interaction lit_…" on its own, an interaction is only
                addressable through the record it belongs to (in the query for
                get/delete, in the body for the PATCH). In bulk update each
                item may carry its own. Optional, and
                purely a convenience, on search for note/reminder/task (same as
                filters={"entity_id": …} / filters={"entity": …}). NOT used by
                op="create": there the parent record goes inside `item`.
            when: op="search" on entity="interaction" only — "past" (default:
                what already happened — the one you want for "what did we say
                to X"), "upcoming" (scheduled ahead), or "all" (both, with
                `past_count`/`upcoming_count` in the receipt). ⚠️ The default
                HIDES upcoming interactions: a `count` under "past" is not the
                total Folk holds for that record.
            object_type: custom-object collection name — `deal` only. Omit it:
                the tool auto-discovers this group's real deal object name
                (tries "deals", and on a 404 reads the correct one out of
                Folk's own error, which enumerates this group's valid entity
                types — same discovery `folk_group(op="custom_fields")` uses).
                Pass it explicitly only if the group has MULTIPLE non-person/
                company custom objects (e.g. Deals AND Events AND Projects) —
                auto-discovery can't guess which one is "deal" and will raise
                asking you to disambiguate.
            dry_run: write ops only — n'écrit RIEN. create: preview
                `would_create`, zéro appel réseau. update / add_to_group : relit
                l'état courant et renvoie un diff `{"changes": {field: {"from",
                "to"}}}` (solo) ou `would_update`/`would_add` (bulk). delete :
                relit chaque record et renvoie `would_delete` (le record actuel),
                pour vérifier ce qui serait détruit avant de le faire. Pour
                `entity="note"` (pas de get-par-id côté Folk), dégrade en
                `{"fields": ..., "current_available": False}` (update) ou un
                record `None` + `"current_available": False` (delete) — aperçu
                sans le "from". Une interaction, elle, a toujours son
                `entity_id` (les trois ops l'exigent), donc son diff est
                toujours réel.
        """
        def _require_deal_group() -> None:
            """Commun aux 5 ops qui acceptent entity="deal" (pas add_to_group,
            qui le refuse d'entrée) : group_id d'abord (raise si absent, AVANT
            tout appel réseau), puis résout `object_type` une seule fois si
            l'appelant ne l'a pas donné. Ne PAS résoudre plus haut (avant le
            dispatch par op) : `add_to_group` rejette entity="deal" sans jamais
            avoir besoin de group_id ni de réseau — y résoudre quand même
            ferait un appel réseau inutile avant un refus qui n'en a pas besoin
            (vécu : cassait `test_add_to_group_deal_entity_rejected`, qui
            n'attend aucun appel client)."""
            nonlocal object_type
            if not group_id:
                raise _bad("group_id requis pour entity='deal'.")
            if object_type is None:
                object_type = _resolve_deal_object_type(_client(), group_id)

        # Un paramètre qui ne s'applique pas à CE couple (op, entity) doit être
        # refusé, jamais ignoré : silencieusement avalé, il fait croire à un
        # filtre appliqué ou à un parent pris en compte. C'est la même famille
        # d'erreur que la claim corrigée en tête de module — une lecture qui
        # rend moins que ce que l'appelant croit avoir demandé.
        if when is not None and not (op == "search" and entity == "interaction"):
            raise _bad("`when` ne vaut que pour op='search' sur "
                       "entity='interaction' (past | upcoming | all).")
        if entity_id is not None and not (
                (op == "search" and entity in ("note", "reminder", "interaction",
                                               "task"))
                or (op in ("get", "delete", "update")
                    and entity == "interaction")):
            raise _bad(
                f"`entity_id` ne vaut pas pour op='{op}' entity='{entity}'. "
                + ("À la création, l'entité porteuse est un CHAMP du record — "
                   "elle peut différer d'un item à l'autre dans un lot — donc "
                   "`item={'entity_id': 'per_…', …}`, pas un paramètre de "
                   "l'appel." if op == "create" else
                   "C'est l'entité PORTEUSE d'une note/interaction/tâche/"
                   "rappel (search), ou celle qui rend une interaction "
                   "adressable (get/update/delete). Pour lister les membres "
                   "d'un groupe : `group_id`."))

        if op == "search":
            if entity not in _SEARCH_ENTITIES:
                raise _bad(f"op='search' : entity doit être l'un de {_SEARCH_ENTITIES}.")
            f = dict(filters or {})
            if entity == "interaction":
                if f:
                    raise _bad(
                        "op='search' entity='interaction' : Folk n'expose aucun "
                        "filtre ici — passer `entity_id` (la personne/société "
                        "porteuse) et, au besoin, `when`.")
                _need(entity_id, "entity_id", "search (entity='interaction')")
                c = _client()
                bucket = when or "past"
                # On tire `max_results + 1` par seau, pas la collection
                # entière : Folk ne filtre pas les interactions et les sert
                # par pages de 30, donc un contact actif en a des centaines
                # (mesuré : >360 sur une seule fiche). Le +1 sert à SAVOIR
                # qu'il en reste sans payer une page de plus pour le dire.
                cap = max_results + 1
                past = (c.list_past_interactions(entity_id, max_items=cap)
                        if bucket in ("past", "all") else [])
                upcoming = (c.list_upcoming_interactions(entity_id, max_items=cap)
                            if bucket in ("upcoming", "all") else [])
                found = past + upcoming
                results = found[:max_results]
                out = {"entity": entity, "when": bucket, "count": len(results),
                       # `count` est ici ce qui est RENDU, pas le total du
                       # workspace : sur les autres entités on connaît le
                       # total parce qu'on a tout tiré, ici on a délibérément
                       # arrêté. Le dire, plutôt que de laisser lire un
                       # `count` comme un inventaire.
                       "truncated": len(found) > max_results,
                       "results": results}
                if bucket == "all":
                    # Les deux listes viennent d'endpoints distincts et le
                    # record ne dit pas de laquelle il sort : sans ce détail,
                    # un `count` agrégé ne se relit pas. `past` est en tête,
                    # donc la césure se déduit de la position.
                    n_past = min(len(past), len(results))
                    out.update(past_count=n_past,
                               upcoming_count=len(results) - n_past)
                return out
            if entity == "task":
                if entity_id:
                    if "entity" in f:
                        raise _bad("passer `entity_id` OU filters={'entity': …}, "
                                   "pas les deux.")
                    f["entity"] = entity_id
                c = _client()
                try:
                    found = c.list_tasks(f)
                except ValueError as e:
                    # Le client valide champs ET opérateurs contre la doc Folk :
                    # sa ValueError nomme déjà ce qui existe, on la rend telle
                    # quelle plutôt qu'en « erreur interne ».
                    raise _bad(str(e))
                return {"entity": entity, "count": len(found),
                        "results": found[:max_results]}
            if entity in ("note", "reminder") and entity_id:
                f.setdefault("entity_id", entity_id)
            if entity in ("note", "reminder"):
                unknown = set(f) - _SUBRECORD_FILTERS
                if unknown:
                    raise _bad(
                        f"op='search' entity='{entity}' : filtre(s) inconnu(s) "
                        f"{sorted(unknown)} — Folk n'expose que "
                        f"{sorted(_SUBRECORD_FILTERS)} sur les notes/rappels.")
                if group_id:
                    raise _bad(
                        f"op='search' entity='{entity}' : Folk ne filtre pas les "
                        "notes/rappels par groupe — passer "
                        "filters={'entity_id': '<person/company/deal id>'}.")
            if entity == "deal":
                _require_deal_group()
            if entity in _GROUP_ENTITIES and group_id:
                # Appartenance à un groupe : le client traduit en filter[groups][in][id].
                f["groups"] = group_id
            c = _client()
            if entity == "person":
                found = c.list_people(**f)
            elif entity == "company":
                found = c.list_companies(**f)
            elif entity == "deal":
                found = c.list_deals(group_id, object_type=object_type, **f)
            elif entity == "note":
                found = c.list_notes(**f)
            else:
                found = c.list_reminders(**f)
            return {"entity": entity, "count": len(found),
                    "results": found[:max_results]}

        if op == "get":
            if entity not in _GET_ENTITIES:
                raise _bad(
                    f"op='get' : entity doit être l'un de {_GET_ENTITIES} — Folk "
                    "n'a pas d'endpoint get-par-id pour les notes (les lister : "
                    "op='search', entity='note').")
            _need(id, "id", op)
            if entity == "deal":
                _require_deal_group()
            if entity in _ENTITY_ID_REQUIRED:
                _need(entity_id, "entity_id", f"{op} (entity='{entity}')")
            return _get_one(_client(), entity, id, group_id=group_id,
                            object_type=object_type, entity_id=entity_id)

        if op == "create":
            if (item is None) == (items is None):
                raise _bad("op='create' : fournir soit `item` (un seul record) soit "
                           "`items` (plusieurs) — pas les deux, pas ni l'un ni l'autre.")
            if entity not in _CREATE_ENTITIES:
                raise _bad(f"op='create' : entity doit être l'un de {_CREATE_ENTITIES}.")
            if entity == "deal":
                _require_deal_group()
            c = _client()
            if item is not None:
                result = _create_one(c, entity, item, group_id=group_id,
                                     object_type=object_type, dry_run=dry_run)
                return {"dry_run": True, **result} if dry_run else result
            results = _bulk_run(
                items, lambda it: _create_one(c, entity, it, group_id=group_id,
                                              object_type=object_type,
                                              dry_run=dry_run))
            failed = [{"index": i, "error": val} for i, ok, val in results if not ok]
            if dry_run:
                would_create = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(items),
                        "would_create": would_create, "failed": failed}
            created = [{"index": i, "id": val.get("id")} for i, ok, val in results if ok]
            return {"total": len(items), "succeeded": len(created),
                    "created": created, "failed": failed}

        if op == "update":
            if (id is None) == (items is None):
                raise _bad("op='update' : fournir soit `id` (+ fields/add_to_groups/"
                           "remove_from_groups) pour UN record, soit `items` pour "
                           "plusieurs — pas les deux, pas ni l'un ni l'autre.")
            if entity not in _UPDATE_ENTITIES:
                raise _bad(f"op='update' : entity doit être l'un de {_UPDATE_ENTITIES}.")
            if entity in _ENTITY_ID_REQUIRED and entity_id is None and not (
                    items and all("entity_id" in it for it in items)):
                # En lot, chaque item peut porter le sien (plusieurs
                # interactions sur des fiches différentes) ; sinon il faut
                # celui de l'appel.
                _need(entity_id, "entity_id", f"{op} (entity='{entity}')")
            if entity == "deal":
                _require_deal_group()
            c = _client()
            if id is not None:
                result = _update_one(
                    c, entity, id, fields=fields, group_id=group_id,
                    object_type=object_type, add_to_groups=add_to_groups,
                    remove_from_groups=remove_from_groups, dry_run=dry_run,
                    entity_id=entity_id)
                return {"dry_run": True, **result} if dry_run else result

            def _one(it):
                if "id" not in it:
                    raise _bad("chaque item doit contenir 'id'.")
                return _update_one(
                    c, entity, it["id"], fields=it.get("fields"),
                    group_id=group_id, object_type=object_type,
                    add_to_groups=it.get("add_to_groups"),
                    remove_from_groups=it.get("remove_from_groups"),
                    dry_run=dry_run, entity_id=it.get("entity_id", entity_id))

            results = _bulk_run(items, _one)
            failed = [{"index": i, "id": items[i].get("id"), "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_update = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(items),
                        "would_update": would_update, "failed": failed}
            return {"total": len(items), "succeeded": len(items) - len(failed),
                    "failed": failed}

        if op == "delete":
            if (id is None) == (ids is None):
                raise _bad("op='delete' : fournir soit `id` (un seul record) soit "
                           "`ids` (plusieurs) — pas les deux, pas ni l'un ni l'autre.")
            if entity not in _DELETE_ENTITIES:
                raise _bad(f"op='delete' : entity doit être l'un de {_DELETE_ENTITIES}.")
            if entity == "deal":
                _require_deal_group()
            if entity in _ENTITY_ID_REQUIRED:
                # Exigé ICI plutôt que dans `_delete_one` : sinon un dry_run
                # sans `entity_id` rendrait un aperçu vide au lieu de dire ce
                # qui manque, et l'appel réel échouerait juste après.
                _need(entity_id, "entity_id", f"{op} (entity='{entity}')")
            c = _client()
            if id is not None:
                result = _delete_one(c, entity, id, group_id=group_id,
                                     object_type=object_type, dry_run=dry_run,
                                     entity_id=entity_id)
                return {"dry_run": True, **result} if dry_run else result
            results = _bulk_run(
                ids, lambda rid: _delete_one(c, entity, rid, group_id=group_id,
                                             object_type=object_type,
                                             dry_run=dry_run,
                                             entity_id=entity_id))
            failed = [{"index": i, "id": ids[i], "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_delete = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(ids),
                        "would_delete": would_delete, "failed": failed}
            return {"total": len(ids), "succeeded": len(ids) - len(failed),
                    "failed": failed}

        if op in ("mark_done", "mark_todo"):
            if entity not in _MARK_ENTITIES:
                raise _bad(f"op='{op}' : entity doit être l'un de "
                           f"{_MARK_ENTITIES} — seule la tâche a une notion de "
                           "complétion (un rappel se déclenche tout seul, il "
                           "ne se termine pas).")
            if (id is None) == (ids is None):
                raise _bad(f"op='{op}' : fournir soit `id` (une seule tâche) "
                           "soit `ids` (plusieurs) — pas les deux, pas ni l'un "
                           "ni l'autre.")
            done = op == "mark_done"
            completed_at = (fields or {}).get("completedAt") if done else None
            unknown = set(fields or {}) - ({"completedAt"} if done else set())
            if unknown:
                raise _bad(
                    f"op='{op}' : champ(s) {sorted(unknown)} refusé(s) ici — "
                    + ("seul `fields={'completedAt': …}` est accepté (défaut : "
                       "maintenant)." if done
                       else "cette op ne prend aucun champ."))
            c = _client()
            if id is not None:
                result = _mark_one(c, id, done, completed_at=completed_at,
                                   dry_run=dry_run)
                return {"dry_run": True, **result} if dry_run else result
            results = _bulk_run(
                ids, lambda tid: _mark_one(c, tid, done,
                                           completed_at=completed_at,
                                           dry_run=dry_run))
            failed = [{"index": i, "id": ids[i], "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_mark = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(ids),
                        "would_mark": would_mark, "failed": failed}
            return {"total": len(ids), "succeeded": len(ids) - len(failed),
                    "failed": failed}

        if op == "add_to_group":
            # Écrit par `_update_one(add_to_groups=[group_id])` : c'est lui qui
            # relit les groupes actuels et réécrit l'union (`groups` est
            # replace-all sur un PATCH Folk). Le contrat rendu à l'appelant est
            # dans le docstring — ici on ne fait que valider et router.
            _need(group_id, "group_id", op)
            if (id is None) == (ids is None):
                raise _bad("op='add_to_group' : fournir soit `id` (un seul record) "
                           "soit `ids` (plusieurs) — pas les deux, pas ni l'un ni "
                           "l'autre.")
            if entity not in _GROUP_ENTITIES:
                raise _bad(f"op='add_to_group' : entity doit être l'un de "
                           f"{_GROUP_ENTITIES}.")
            c = _client()
            if id is not None:
                result = _update_one(c, entity, id, add_to_groups=[group_id],
                                     dry_run=dry_run)
                return {"dry_run": True, **result} if dry_run else result
            results = _bulk_run(
                ids, lambda rid: _update_one(c, entity, rid, add_to_groups=[group_id],
                                             dry_run=dry_run))
            failed = [{"index": i, "id": ids[i], "error": val}
                      for i, ok, val in results if not ok]
            if dry_run:
                would_add = [{"index": i, **val} for i, ok, val in results if ok]
                return {"dry_run": True, "total": len(ids), "would_add": would_add,
                        "failed": failed}
            return {"total": len(ids), "succeeded": len(ids) - len(failed),
                    "failed": failed}

        raise _bad("op doit être 'search', 'get', 'create', 'update', 'delete', "
                   "'add_to_group', 'mark_done' ou 'mark_todo'")

    # --- groups + group custom fields + group members -------------------------
    #
    # Pas de "get a group" côté API Folk (seuls list/create/update existent) :
    # le dry_run d'op="update"/"remove_member"/"update_member" relit list_groups()/
    # list_group_members() et filtre sur l'id, même limitation déjà rencontrée
    # sur notes/reminders (pas de filtre serveur, filtré côté client).

    @mcp.tool()
    def folk_group(
        op: Literal[
            "list", "create", "update",
            "custom_fields", "get_custom_field",
            "create_custom_field", "update_custom_field",
            "members", "add_member", "remove_member", "update_member",
        ] = "list",
        group_id: Optional[str] = None,
        entity_type: str = "person",
        custom_field_name: Optional[str] = None,
        name: Optional[str] = None,
        visibility: Optional[Literal["public", "private"]] = None,
        custom_field: Optional[dict] = None,
        fields: Optional[dict] = None,
        user_id: Optional[str] = None,
        role: Optional[Literal["admin", "contributor", "reader"]] = None,
        dry_run: bool = False,
    ) -> dict:
        """A Folk group (a folder of people/companies/deals), the custom fields
        defined on it, and its members — list/create/update any of the three.

        `op`:
        - **"list"** (default): list all groups in the Folk workspace.
        - **"create"**: create a group (`name` + `visibility`). A default "All
          people" table view is created automatically by Folk.
        - **"update"**: PATCH a group (`group_id` + `name` and/or `visibility`).
        - **"custom_fields"**: list the custom fields defined on a group for an
          entity type (`group_id` + `entity_type`).
        - **"get_custom_field"**: read one custom field by name (`group_id` +
          `entity_type` + `custom_field_name`).
        - **"create_custom_field"**: create a custom field on a group for an
          entity type (`group_id` + `entity_type` + `custom_field`).
        - **"update_custom_field"**: PATCH a custom field (`group_id` +
          `entity_type` + `custom_field_name` + `fields`).
        - **"members"**: list a group's members (`group_id`) — id, full name,
          email, role. On a **public** group this lists EVERY workspace user
          (Folk auto-membership, role always "admin"), not just people
          explicitly added — see note below.
        - **"add_member"**: add a workspace user to a group (`group_id` +
          `user_id` + `role`). Get `user_id` from `folk_user(op="list")`. A
          no-op on a public group (see note below) — the target is already an
          implicit member.
        - **"remove_member"**: remove a member from a group (`group_id` +
          `user_id`).
        - **"update_member"**: change a member's role (`group_id` + `user_id` +
          `role`).

        Note: Folk has no delete endpoint for a group or a custom field — remove
        either from the Folk app, not from here. A group MEMBER, unlike the
        group itself, can be removed via the API (`op="remove_member"`).

        Note: **`visibility="public"` makes membership implicit and workspace-
        wide** (confirmed live, 2026-08-17) — EVERY workspace user shows up in
        `op="members"` with role "admin", whether or not anyone explicitly
        added them. `op="add_member"`/`"remove_member"`/`"update_member"` only
        do something meaningful on a **private** group (explicit membership).
        To manage membership deliberately, create/update the group with
        `visibility="private"` first.

        Args:
            op: list (default) | create | update | custom_fields |
                get_custom_field | create_custom_field | update_custom_field |
                members | add_member | remove_member | update_member.
            group_id: required for every op except "list"/"create".
            entity_type: "person", "company", or a custom object's DISPLAY
                name — required for every custom-field op (default "person").
                Not used by member ops. ⚠️ Only "person"/"company" are fixed.
                Everything else is a **custom object each Folk customer names
                themselves** — "Deals" is just what THIS workspace happens to
                call theirs; another workspace's equivalent could be
                "Opportunities", "Transactions", singular, translated,
                anything. Never assume a name. Discover it: call
                `op="custom_fields"` with any placeholder entity_type — Folk's
                404 names the group's REAL entity types (`"Available entity
                types are: ..."`), then reissue with the right one. This is
                also the exact, case-sensitive display name — NOT the
                lowercase URL slug `folk_record`/`folk_webhook` call
                `object_type` (confirmed live: on one real workspace,
                `object_type="deals"` 404s while `object_type="Deals"`,
                matching this workspace's custom object name, returns
                records) — the two params can differ even for the same
                collection.
            custom_field_name: op="get_custom_field"/"update_custom_field" — the
                field's `name` (custom fields have no separate id; `name` IS the
                identifier Folk matches on, from op="custom_fields").
            name: op="create"/"update" — group name (1-255 chars).
            visibility: op="create"/"update" — "public" (every workspace user
                is an implicit member, role "admin" — see note above) or
                "private" (only explicitly added members). Required on create.
            custom_field: op="create_custom_field" — the field body, discriminated
                by `type`:
                  textField / dateField / userField / contactField / objectField:
                    `{"type": ..., "name": ...}`
                  singleSelect / multipleSelect:
                    `{"type": ..., "name": ..., "options": [{"label", "color"}]}`
                    (color is one of #5738ff #20cea9 #f54e50 #f2b934 #879aab
                    #de4a96 #4a90e2 #f5a623)
                  numericField:
                    `{"type": "numericField", "name": ...,
                      "config": {"format": "default"|"percent"|"currency"|"none"|"number",
                                 "decimals"?: 0-5, "currency"?: "EUR"}}`
                    (currency required only when format="currency")
            fields: op="update_custom_field" — any of `name`, `config` (same
                shape as `custom_field.config` above), `addOptions`
                (`[{"label", "color"}]`, 1-100), `removeOptions` (option ids,
                1-100), `updateOptions` (`[{"id", "label"?, "color"?}]`, 1-100) —
                only the given ones change.
            user_id: op="add_member"/"remove_member"/"update_member" — the
                workspace user id (from `folk_user(op="list")`, NOT `folk_group
                (op="members")` — a group member IS a workspace user).
            role: op="add_member"/"update_member" — required, one of "admin",
                "contributor", "reader". A LIST can also show "owner" (Folk's
                workspace owner) but it can't be SET through this API.
            dry_run: op="create"/"create_custom_field"/"add_member" — preview
                (`would_create`/`would_add`), zero network calls. op="update"/
                "update_custom_field"/"update_member" — diff `{"changes":
                {field: {"from", "to"}}}` against the current record.
                op="remove_member" — preview (`would_remove`) of the member
                that would be removed, zero network calls.
        """
        if op == "list":
            return {"groups": _client().list_groups()}

        if op == "create":
            _need(name, "name", op)
            _need(visibility, "visibility", op)
            if dry_run:
                return {"dry_run": True,
                         "would_create": {"name": name, "visibility": visibility}}
            return _client().create_group(name, visibility)

        if op == "update":
            _need(group_id, "group_id", op)
            group_fields: dict = {}
            if name is not None:
                group_fields["name"] = name
            if visibility is not None:
                group_fields["visibility"] = visibility
            if not group_fields:
                raise _bad("op='update' requiert name et/ou visibility "
                           "(pas `fields` — réservé à op='update_custom_field').")
            c = _client()
            if dry_run:
                current = next((g for g in c.list_groups() if g.get("id") == group_id), None)
                if current is None:
                    raise _bad(f"group_id {group_id!r} introuvable (folk_group op='list').")
                return {"dry_run": True, "group_id": group_id,
                         "changes": {k: {"from": current.get(k), "to": v}
                                     for k, v in group_fields.items()}}
            return c.update_group(group_id, **group_fields)

        if op == "custom_fields":
            _need(group_id, "group_id", op)
            return {"custom_fields": _client().get_group_custom_fields(
                group_id, entity_type)}

        if op == "get_custom_field":
            _need(group_id, "group_id", op)
            _need(custom_field_name, "custom_field_name", op)
            return _client().get_group_custom_field(group_id, entity_type, custom_field_name)

        if op == "create_custom_field":
            _need(group_id, "group_id", op)
            _need(custom_field, "custom_field", op)
            _reject_reserved_keys(custom_field, "custom_field", op)
            if dry_run:
                return {"dry_run": True, "would_create": custom_field}
            return _client().create_group_custom_field(group_id, entity_type, **custom_field)

        if op == "update_custom_field":
            _need(group_id, "group_id", op)
            _need(custom_field_name, "custom_field_name", op)
            if not fields:
                raise _bad("op='update_custom_field' requiert fields : au moins un "
                           "champ (name, config, addOptions, removeOptions, "
                           "updateOptions).")
            _reject_reserved_keys(fields, "fields", op)
            c = _client()
            if dry_run:
                current = c.get_group_custom_field(group_id, entity_type, custom_field_name)
                return {"dry_run": True, "custom_field_name": custom_field_name,
                         "changes": {k: {"from": current.get(k), "to": v}
                                     for k, v in fields.items()}}
            return c.update_group_custom_field(group_id, entity_type, custom_field_name, **fields)

        if op == "members":
            _need(group_id, "group_id", op)
            return {"members": _client().list_group_members(group_id)}

        if op == "add_member":
            _need(group_id, "group_id", op)
            _need(user_id, "user_id", op)
            _need(role, "role", op)
            if dry_run:
                return {"dry_run": True, "would_add": {"id": user_id, "role": role}}
            return _client().add_group_member(group_id, user_id, role)

        if op == "remove_member":
            _need(group_id, "group_id", op)
            _need(user_id, "user_id", op)
            c = _client()
            if dry_run:
                current = next(
                    (m for m in c.list_group_members(group_id) if m.get("id") == user_id), None)
                if current is None:
                    raise _bad(f"user_id {user_id!r} introuvable dans ce groupe "
                               "(folk_group op='members').")
                return {"dry_run": True, "would_remove": current}
            return c.remove_group_member(group_id, user_id)

        if op == "update_member":
            _need(group_id, "group_id", op)
            _need(user_id, "user_id", op)
            _need(role, "role", op)
            c = _client()
            if dry_run:
                current = next(
                    (m for m in c.list_group_members(group_id) if m.get("id") == user_id), None)
                if current is None:
                    raise _bad(f"user_id {user_id!r} introuvable dans ce groupe "
                               "(folk_group op='members').")
                return {"dry_run": True, "user_id": user_id,
                         "changes": {"role": {"from": current.get("role"), "to": role}}}
            return c.update_group_member(group_id, user_id, role)

        raise _bad("op doit être l'un de 'list', 'create', 'update', 'custom_fields', "
                   "'get_custom_field', 'create_custom_field', 'update_custom_field', "
                   "'members', 'add_member', 'remove_member', 'update_member'")

    # --- users (membres du workspace, lecture seule) ------------------------

    @mcp.tool()
    def folk_user(op: Literal["list", "get"] = "list", user_id: str = "me") -> dict:
        """A Folk workspace user (member) — list them, or fetch one.

        `op`:
        - **"list"** (default): list the workspace users (members) — useful to
          resolve owners/assignees.
        - **"get"**: fetch a workspace user by ID. `user_id="me"` (default)
          returns the authenticated user — call it to attribute an action to the
          current user.

        Args:
            op: list (default) | get.
            user_id: op="get" — the user ID, or "me" (default).
        """
        if op == "list":
            return {"users": _client().list_users()}
        if op == "get":
            return _client().get_user(user_id)
        raise _bad("op doit être 'list' ou 'get'")

    # --- webhooks -------------------------------------------------------------
    #
    # Ressource globale (pas d'`entity`, pas de group_id/object_type, pas de
    # mode bulk — un workspace en a peu). `dry_run` suit la même convention que
    # `folk_record` (preview `would_create` en création, diff `changes` en
    # update, aucun appel réseau mutant).

    @mcp.tool()
    def folk_webhook(
        op: Literal["list", "create", "update"] = "list",
        webhook_id: Optional[str] = None,
        name: Optional[str] = None,
        target_url: Optional[str] = None,
        subscribed_events: Optional[list[dict]] = None,
        fields: Optional[dict] = None,
        dry_run: bool = False,
    ) -> dict:
        """A Folk webhook — list, create, update. Folk POSTs an event payload to
        `target_url` each time one of the subscribed events fires.

        `op`:
        - **"list"** (default): list all webhooks configured on this Folk
          workspace: target URL, status, and which events/filters each one
          subscribes to.
        - **"create"**: create a webhook (`name` + `target_url` +
          `subscribed_events`).
        - **"update"**: PATCH a webhook (`webhook_id` + `fields`) — only the
          given fields change.

        Before creating with a filter, call `folk_group` (op="list" for
        `groupId`, op="custom_fields" for the custom field name used in `path`)
        to get real workspace values — don't guess them.

        Note: on create, the response's `signingSecret` is returned in FULL only
        there — Folk only ever shows a redacted version afterwards, so save it
        now if you need to verify payload signatures.

        Note: filters only exist through this API — editing a webhook's events
        from Folk's own settings UI afterwards silently drops them.

        Args:
            op: list (default) | create | update.
            webhook_id: op="update" — the webhook ID (wbk_…, from op="list").
            name: op="create" — friendly name (max 255 chars).
            target_url: op="create" — public HTTPS URL that will receive the
                event (max 2048 chars).
            subscribed_events: op="create" — 1-20 items, each
                `{"eventType": ..., "filter": {...}}`.
                eventType — one per entity, by lifecycle:
                  person: created, updated, deleted, groups_updated,
                    workspace_interaction_metadata_updated
                  company: created, updated, deleted, groups_updated
                  object (deals AND any custom object_type): created, updated, deleted
                  note: created, updated, deleted
                  reminder: created, updated, deleted, triggered
                (full values are "person.created", "object.updated", etc.)
                filter (optional, all keys optional):
                  groupId — only for entities in this group (`folk_group`).
                    For object.* this is a sibling of `path`, never repeated
                    inside it.
                  objectType — for object.* events, scope to one collection
                    (e.g. "Deals" vs a custom object_type). Confirmed against a
                    live workspace: this is the exact display name, same as
                    `folk_group`'s `entity_type` — NOT the lowercase slug
                    `folk_record(entity="deal", object_type=...)` historically
                    defaulted to (that tool now auto-discovers it; here, pass
                    the real name yourself).
                  path + value — for *.updated events, fire only when the
                    attribute at `path` changes to `value`. `path` covers both
                    plain attributes and custom fields, and its shape differs
                    by entity:
                      plain attribute (any entity): `["firstName"]`, `["name"]`
                      person/company custom field:
                        `["customFieldValues", groupId, fieldName]` (3 segments
                        — the group id is repeated here, inside the path)
                      object/deal custom field:
                        `["customFieldValues", fieldName]` (2 segments — no
                        group id in path; use the sibling `filter.groupId`
                        instead)
                    fieldName is the field's `name` from
                    `folk_group(op="custom_fields")` (custom fields have no
                    separate id — `name` IS the identifier Folk matches on).
            fields: op="update" — Folk's raw API field names, camelCase — same
                vocabulary caveat as `folk_record(op="update")`: name, targetUrl,
                subscribedEvents (REPLACES the full list, not a merge/add — call
                op="list" first and resend the existing entries you want to
                keep), status ("active"|"inactive" — pause without deleting).
                Same eventType/filter shape as op="create".
            dry_run: if true, writes nothing — op="create" returns a preview
                (`would_create`), zero network calls; op="update" returns a diff
                `{"changes": {field: {"from", "to"}}}` against the current
                webhook.
        """
        if op == "list":
            return {"webhooks": _client().list_webhooks()}

        if op == "create":
            _need(name, "name", op)
            _need(target_url, "target_url", op)
            _need(subscribed_events, "subscribed_events", op)
            _validate_subscribed_events(subscribed_events)
            if dry_run:
                return {"dry_run": True, "would_create": {
                    "name": name, "targetUrl": target_url,
                    "subscribedEvents": subscribed_events,
                }}
            return _client().create_webhook(name, target_url, subscribed_events)

        if op == "update":
            _need(webhook_id, "webhook_id", op)
            if not fields:
                raise _bad("op='update' requiert fields : au moins un champ à mettre "
                           "à jour (name, targetUrl, subscribedEvents, status).")
            if "subscribedEvents" in fields:
                _validate_subscribed_events(fields["subscribedEvents"])
            c = _client()
            if dry_run:
                current = c.get_webhook(webhook_id)
                return {"dry_run": True, "id": webhook_id,
                        "changes": {k: {"from": current.get(k), "to": v}
                                    for k, v in fields.items()}}
            return c.update_webhook(webhook_id, **fields)

        raise _bad("op doit être 'list', 'create' ou 'update'")
