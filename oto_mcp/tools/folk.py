"""Folk CRM — groups, people, companies, deals, notes, interactions, reminders.

Wrappe `oto.tools.folk.FolkClient` (API publique https://developer.folk.app).
Clé résolue par appel via `access.resolve_api_key("folk")` — provider byo-only
(user key posée sur /account, ou credential partagé de l'org active). Pas de
clé plateforme.

Surface : lecture/écriture **par entité** (`folk_search`/`get` prennent
`entity` = person|company|deal). `folk_create`/`update`/`delete`/
`add_to_group` couvrent aussi note/reminder (et interaction pour create), et
sont **solo OU bulk selon le param passé** : un singulier (`item`/`id`) pour
UN record → résultat direct ; un pluriel (`items`/`ids`, ≤50) pour plusieurs →
reçu allégé (compte + erreurs par item, jamais N corps de réponse complets).
Folk n'a d'endpoint batch nulle part — le mode bulk boucle sur les méthodes
single-record avec un petit délai de courtoisie (`_bulk_run`).

⚠️ **Deux vocabulaires de champs différents cohabitent** : `folk_create` prend
des clés Python snake_case (`first_name`, `company_id`...) ; `folk_update`
prend les noms de champs bruts de l'API Folk en camelCase (`jobTitle`,
`customFieldValues`...). Ne pas transposer l'un vers l'autre — voir le
docstring de chaque tool.
"""
from __future__ import annotations

import time
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


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


# --- dispatch par entité, partagé entre outils singuliers et bulk -----------
#
# `_create_one`/`_update_one`/`_delete_one` portent la logique auparavant
# écrite en dur dans chaque outil singulier (`folk_update`, `folk_delete`,
# `folk_create_*`) : on l'extrait pour que les outils bulk l'appellent
# item-par-item sans dupliquer/diverger de la validation. Tous les trois
# acceptent `dry_run` (convention oto — cf. `email_send`, LinkedIn
# `send_message`/`connect`) : la validation tourne normalement, seul l'appel
# mutant final est sauté, remplacé par un aperçu.

_CREATE_ENTITIES = ("person", "company", "deal", "note", "interaction", "reminder")
_UPDATE_ENTITIES = ("person", "company", "deal", "note", "reminder")
_DELETE_ENTITIES = ("person", "company", "deal", "note", "reminder")
_GROUP_ENTITIES = ("person", "company")

# Champs acceptés par `folk_create` par entité — miroir des paramètres nommés
# des méthodes `FolkClient.create_*` (snake_case Python, PAS les noms de
# champs API Folk en camelCase utilisés par `folk_update`/`fields`). Codé en
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
    "reminder": {"entity_id", "name", "recurrence_rule", "visibility"},
}


def _get_one(c, entity: str, id: str, group_id: Optional[str] = None,
             object_type: str = "deals"):
    """Récupère l'état courant d'un record, pour diff/preview `dry_run`.

    Renvoie `None` pour `note` : Folk n'a PAS d'endpoint get-par-id pour les
    notes (`client.py` n'expose que list/create/update/delete) — un gap
    permanent de l'API, pas un raccourci d'implémentation. Les previews
    update/delete d'une note dégradent en conséquence (pas de diff possible)."""
    if entity == "person":
        return c.get_person(id)
    if entity == "company":
        return c.get_company(id)
    if entity == "deal":
        if not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        return c.get_deal(group_id, id, object_type=object_type)
    if entity == "reminder":
        return c.get_reminder(id)
    return None


def _create_one(c, entity: str, group_id: Optional[str] = None,
                 object_type: str = "deals", dry_run: bool = False, **fields):
    if entity == "deal" and not group_id:
        raise _bad("group_id requis pour entity='deal'.")
    unknown = set(fields) - _CREATE_FIELDS.get(entity, set())
    if unknown:
        raise _bad(
            f"champ(s) inconnu(s) pour entity='{entity}' : {sorted(unknown)}. "
            f"Champs acceptés : {sorted(_CREATE_FIELDS.get(entity, set()))}. "
            f"Rappel : folk_create utilise des clés snake_case Python "
            f"(first_name, company_id...) — PAS les noms de champs API Folk "
            f"en camelCase (jobTitle, customFieldValues...) utilisés par folk_update.")
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
    if entity == "reminder":
        return c.create_reminder(**fields)
    raise _bad(f"entity doit être l'un de {_CREATE_ENTITIES}.")


def _update_one(c, entity: str, id: str, fields: Optional[dict] = None,
                 group_id: Optional[str] = None, object_type: str = "deals",
                 add_to_groups: Optional[list[str]] = None,
                 remove_from_groups: Optional[list[str]] = None,
                 dry_run: bool = False):
    fields = dict(fields or {})
    current = None
    if add_to_groups or remove_from_groups or dry_run:
        current = _get_one(c, entity, id, group_id=group_id, object_type=object_type)
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
    if entity == "reminder":
        return c.update_reminder(id, **fields)
    raise _bad(f"entity doit être l'un de {_UPDATE_ENTITIES}.")


def _delete_one(c, entity: str, id: str, group_id: Optional[str] = None,
                 object_type: str = "deals", dry_run: bool = False):
    if dry_run:
        current = _get_one(c, entity, id, group_id=group_id, object_type=object_type)
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
    if entity == "reminder":
        return c.delete_reminder(id)
    raise _bad(f"entity doit être l'un de {_DELETE_ENTITIES}.")


# Cap dérivé du timeout dur de l'invocation REST d'un outil (`api_routes.py`,
# `asyncio.wait_for(_invoke(), timeout=45)`) : à ~0.15s de délai de courtoisie
# entre appels + latence Folk par item, on vise une marge confortable sous
# les 45s plutôt que de s'en approcher.
_BULK_MAX_ITEMS = 50
_BULK_DELAY_S = 0.15


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
    """Exécute `fn(item)` pour chaque item, avec un petit délai de courtoisie
    entre appels (limite de débit Folk — `_request` gère déjà les 429, ce
    délai évite de les déclencher trop souvent). Renvoie une liste de
    `(index, ok, valeur_ou_message_erreur)` — au tool d'en tirer le reçu
    (les formes diffèrent : create doit remonter les IDs créés, les autres
    juste un compte)."""
    if len(items) > _BULK_MAX_ITEMS:
        raise _bad(f"trop d'éléments ({len(items)}) — max {_BULK_MAX_ITEMS} par appel, "
                   f"découper en plusieurs appels.")
    results: list[tuple[int, bool, object]] = []
    for i, item in enumerate(items):
        try:
            results.append((i, True, fn(item)))
        except Exception as e:
            if _bulk_fatal(e):
                raise
            results.append((i, False, str(e)))
        if i < len(items) - 1:
            time.sleep(_BULK_DELAY_S)
    return results


def register(mcp: FastMCP) -> None:
    from oto.tools.folk.client import FolkClient

    def _client() -> FolkClient:
        key, _ = access.resolve_api_key("folk")
        # Rédaction des champs sensibles : plus au niveau client — appliquée à la
        # frontière des tools par `FieldRedactionMiddleware` (policy de l'org active).
        return FolkClient(api_key=key)

    # --- groups -------------------------------------------------------------

    @mcp.tool()
    def folk_list_groups() -> dict:
        """List all groups in the Folk workspace (a group = a folder of people/companies/deals).

        Note: the Folk API is read-only on groups — there is no endpoint to
        create one. A new group must be created by the user in the Folk app,
        then referenced here by its ID.
        """
        return {"groups": _client().list_groups()}

    @mcp.tool()
    def folk_group_custom_fields(group_id: str, entity_type: str = "person") -> dict:
        """List the custom fields defined on a group for an entity type.

        Args:
            group_id: Folk group ID.
            entity_type: "person" or "company".
        """
        return {"custom_fields": _client().get_group_custom_fields(group_id, entity_type)}

    # --- lecture/écriture par entité (person | company [| deal]) -------------

    @mcp.tool()
    def folk_search(
        entity: str, filters: Optional[dict] = None, max_results: int = 100,
        group_id: Optional[str] = None, object_type: str = "deals",
    ) -> dict:
        """Search Folk records. Fetches ALL matching pages — always pass filters on
        a large workspace.

        Args:
            entity: "person", "company" or "deal".
            filters: Field → value, matched with `like` (e.g. {"fullName": "Dupont",
                "emails": "@otomata.tech"} for people, {"name": "Otomata"} for companies).
            max_results: Truncate the response (default 100).
            group_id: REQUIRED for `deal` only (the group whose deals to search).
            object_type: collection name (default "deals"), `deal` only.
        """
        c = _client()
        if entity == "person":
            items = c.list_people(**(filters or {}))
        elif entity == "company":
            items = c.list_companies(**(filters or {}))
        elif entity == "deal":
            if not group_id:
                raise _bad("group_id requis pour entity='deal'.")
            items = c.list_deals(group_id, object_type=object_type, **(filters or {}))
        else:
            raise _bad("entity doit être 'person', 'company' ou 'deal'.")
        return {"entity": entity, "count": len(items), "results": items[:max_results]}

    @mcp.tool()
    def folk_get(
        entity: str, id: str,
        group_id: Optional[str] = None, object_type: str = "deals",
    ) -> dict:
        """Fetch a Folk record by ID (full record).

        Args:
            entity: "person", "company" or "deal".
            id: the record ID (the deal_id for a deal).
            group_id: REQUIRED for `deal` only (the group where the deal lives).
            object_type: collection name (default "deals"), `deal` only.
        """
        c = _client()
        if entity == "person":
            return c.get_person(id)
        if entity == "company":
            return c.get_company(id)
        if entity == "deal":
            if not group_id:
                raise _bad("group_id requis pour entity='deal'.")
            return c.get_deal(group_id, id, object_type=object_type)
        raise _bad("entity doit être 'person', 'company' ou 'deal'.")

    @mcp.tool()
    def folk_get_reminder(reminder_id: str) -> dict:
        """Fetch a Folk reminder by ID (full record). `reminder_id` = rmd_…."""
        return _client().get_reminder(reminder_id)

    # --- create/update/delete/add_to_group : un tool par verbe, solo OU bulk -
    #
    # Chaque tool prend une paire de params mutuellement exclusifs : le
    # singulier (un seul record, résultat/preview renvoyé directement) OU le
    # pluriel (jusqu'à 50, reçu bulk). Folk n'a d'endpoint batch nulle part
    # (vérifié sur ce connecteur, le MCP officiel Folk, et un MCP tiers) — le
    # pluriel boucle sur les méthodes single-record avec un délai de
    # courtoisie (`_bulk_run`) et renvoie un reçu allégé, jamais N corps de
    # réponse complets.

    @mcp.tool()
    def folk_create(
        entity: str,
        item: Optional[dict] = None,
        items: Optional[list[dict]] = None,
        group_id: Optional[str] = None,
        object_type: str = "deals",
        dry_run: bool = False,
    ) -> dict:
        """Create one or several Folk records of the same entity type.

        Pass `item` for ONE record (returns the created record directly, or
        the dry_run preview). Pass `items` (up to 50) for several (returns a
        receipt). Exactly one of `item`/`items` is required.

        ⚠️ Field names here are Python **snake_case** parameter names
        (`first_name`, `company_id`...), forwarded directly to the client —
        NOT Folk's raw camelCase API field vocabulary (`jobTitle`,
        `customFieldValues`...) that `folk_update`'s `fields` uses. An
        unrecognized field name raises immediately (listing the accepted
        ones), it is never silently dropped or sent under the wrong name.

        Args:
            entity: "person", "company", "deal", "note", "interaction" or "reminder".
            item: fields for ONE record — see the per-entity shape below.
            items: fields for MULTIPLE records, same shape as `item`, one
                dict per record.
            group_id: REQUIRED for `entity="deal"` only — all record(s) land
                in this one group (Folk deals aren't creatable across groups
                in a single call).
            object_type: collection name (default "deals"), `deal` only.
            dry_run: if true, creates nothing — returns a preview instead
                (`would_create`); zero network calls.

        Per-entity field shape (same for `item` and each entry of `items`,
        `*` = required, snake_case — see the warning above):
            person: {first_name*, last_name, emails, phones, job_title,
                company_name, company_id, group_ids, urls, description}
            company: {name*, emails, industry}
            deal: {name*, people_ids, company_ids, custom_fields}
            note: {entity_id*, content*, visibility}
            interaction: {entity_id*, type*, title*, content, date_time}
            reminder: {entity_id*, name*, recurrence_rule*, visibility}

        Returns:
            item: the created record, or {"dry_run": true, "would_create": {...}}.
            items: {"total", "succeeded", "created": [{"index","id"}],
                "failed": [...]}, or dry_run: {"dry_run": true, "total",
                "would_create": [...], "failed": [...]}.
        """
        if (item is None) == (items is None):
            raise _bad("fournir soit `item` (un seul record) soit `items` "
                       "(plusieurs) — pas les deux, pas ni l'un ni l'autre.")
        if entity not in _CREATE_ENTITIES:
            raise _bad(f"entity doit être l'un de {_CREATE_ENTITIES}.")
        if entity == "deal" and not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        c = _client()
        if item is not None:
            result = _create_one(c, entity, group_id=group_id, object_type=object_type,
                                 dry_run=dry_run, **item)
            return {"dry_run": True, **result} if dry_run else result
        results = _bulk_run(
            items, lambda it: _create_one(c, entity, group_id=group_id,
                                          object_type=object_type,
                                          dry_run=dry_run, **it))
        failed = [{"index": i, "error": val} for i, ok, val in results if not ok]
        if dry_run:
            would_create = [{"index": i, **val} for i, ok, val in results if ok]
            return {"dry_run": True, "total": len(items), "would_create": would_create,
                    "failed": failed}
        created = [{"index": i, "id": val.get("id")} for i, ok, val in results if ok]
        return {"total": len(items), "succeeded": len(created),
                "created": created, "failed": failed}

    @mcp.tool()
    def folk_update(
        entity: str,
        id: Optional[str] = None,
        fields: Optional[dict] = None,
        add_to_groups: Optional[list[str]] = None,
        remove_from_groups: Optional[list[str]] = None,
        items: Optional[list[dict]] = None,
        group_id: Optional[str] = None,
        object_type: str = "deals",
        dry_run: bool = False,
    ) -> dict:
        """Update one or several Folk records of the same entity type (PATCH
        — only the given fields change).

        Pass `id` (+ `fields`/`add_to_groups`/`remove_from_groups`) for ONE
        record (returns the record directly, or the dry_run diff). Pass
        `items` (up to 50, each `{"id", "fields", "add_to_groups",
        "remove_from_groups"}`) for several (returns a receipt). Exactly one
        of `id`/`items` is required.

        ⚠️ Field names in `fields` are Folk's raw **camelCase** API vocabulary
        (`jobTitle`, `customFieldValues`...), passed through as-is — a
        DIFFERENT vocabulary from `folk_create`'s snake_case Python parameter
        names (`first_name`, `company_id`...). Don't mix the two conventions.

        Args:
            entity: "person", "company", "deal", "note" or "reminder"
                (interactions have no update endpoint in Folk).
            id: the record ID (the deal_id for a deal) — solo mode.
            fields: Folk API field names, camelCase (e.g. {"jobTitle": "CTO"},
                {"industry": "SaaS"}, ou champs custom d'un deal). Optionnel si
                seuls `add_to_groups`/`remove_from_groups` sont fournis.
                **Champs CUSTOM d'une person/company** (ex. Status d'un groupe) :
                les passer SOUS `customFieldValues`, keyés par group_id —
                `{"customFieldValues": {"<group_id>": {"Status": "Follow-up"}}}`.
                Un champ custom passé à plat (`{"Status": …}`) est rejeté (422
                "Unrecognized key"). La structure se découvre via folk_search
                (customFieldValues groupée par group_id).
            add_to_groups / remove_from_groups: rattacher/détacher une **person**
                ou **company** À des groupes (folk_list_groups pour les IDs),
                sans toucher ses autres groupes — solo mode only.
            items: {"id", "fields", "add_to_groups", "remove_from_groups"} par
                record — bulk mode, même vocabulaire de champs que ci-dessus.
            group_id: REQUIRED for `entity="deal"` only (le/les groupe(s) où
                vivent ces deals). Ne PAS le passer pour person/company.
            object_type: nom de la collection (défaut "deals"), `deal` seulement.
            dry_run: si vrai, ne PATCH rien — renvoie un diff `{"changes":
                {field: {"from", "to"}}}` (solo) ou `would_update` (bulk), en
                relisant l'état courant. Pour `entity="note"` (pas de get-par-id
                côté Folk), dégrade en `{"fields": ..., "current_available":
                False}` — aperçu sans le "from".

        Returns:
            id: the updated record, or {"dry_run": true, "id", "changes"|"fields", ...}.
            items: {"total", "succeeded", "failed": [{"index","id","error"}]},
                or dry_run: {"dry_run": true, "total", "would_update": [...], "failed": [...]}.
        """
        if (id is None) == (items is None):
            raise _bad("fournir soit `id` (+ fields/add_to_groups/remove_from_groups) "
                       "pour UN record, soit `items` pour plusieurs — pas les deux, "
                       "pas ni l'un ni l'autre.")
        if entity not in _UPDATE_ENTITIES:
            raise _bad(f"entity doit être l'un de {_UPDATE_ENTITIES}.")
        if entity == "deal" and not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        c = _client()
        if id is not None:
            result = _update_one(
                c, entity, id, fields=fields, group_id=group_id,
                object_type=object_type, add_to_groups=add_to_groups,
                remove_from_groups=remove_from_groups, dry_run=dry_run)
            return {"dry_run": True, **result} if dry_run else result

        def _one(it):
            if "id" not in it:
                raise _bad("chaque item doit contenir 'id'.")
            return _update_one(
                c, entity, it["id"], fields=it.get("fields"),
                group_id=group_id, object_type=object_type,
                add_to_groups=it.get("add_to_groups"),
                remove_from_groups=it.get("remove_from_groups"),
                dry_run=dry_run)

        results = _bulk_run(items, _one)
        failed = [{"index": i, "id": items[i].get("id"), "error": val}
                 for i, ok, val in results if not ok]
        if dry_run:
            would_update = [{"index": i, **val} for i, ok, val in results if ok]
            return {"dry_run": True, "total": len(items), "would_update": would_update,
                    "failed": failed}
        return {"total": len(items), "succeeded": len(items) - len(failed), "failed": failed}

    @mcp.tool()
    def folk_delete(
        entity: str,
        id: Optional[str] = None,
        ids: Optional[list[str]] = None,
        group_id: Optional[str] = None,
        object_type: str = "deals",
        dry_run: bool = False,
    ) -> dict:
        """Delete one or several Folk records. Irreversible.

        Pass `id` for ONE record. Pass `ids` (up to 50) for several (returns
        a receipt). Exactly one of `id`/`ids` is required.

        Args:
            entity: "person", "company", "deal", "note" or "reminder"
                (interactions have no delete endpoint in Folk).
            id: the record ID (the deal_id for a deal) — solo mode.
            ids: record IDs — bulk mode (deal IDs for entity="deal").
            group_id: REQUIRED for `entity="deal"` only.
            object_type: collection name (default "deals"), `deal` only.
            dry_run: si vrai, ne supprime RIEN — relit chaque record et
                renvoie `would_delete` (le record actuel), pour vérifier ce
                qui serait détruit avant de le faire. Pour `entity="note"`
                (pas de get-par-id côté Folk), le record est `None` +
                `"current_available": False`.

        Returns:
            id: {} (or {"dry_run": true, "id", "would_delete", ...}).
            ids: {"total", "succeeded", "failed": [{"index","id","error"}]},
                or dry_run: {"dry_run": true, "total", "would_delete": [...], "failed": [...]}.
        """
        if (id is None) == (ids is None):
            raise _bad("fournir soit `id` (un seul record) soit `ids` (plusieurs) "
                       "— pas les deux, pas ni l'un ni l'autre.")
        if entity not in _DELETE_ENTITIES:
            raise _bad(f"entity doit être l'un de {_DELETE_ENTITIES}.")
        if entity == "deal" and not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        c = _client()
        if id is not None:
            result = _delete_one(c, entity, id, group_id=group_id,
                                 object_type=object_type, dry_run=dry_run)
            return {"dry_run": True, **result} if dry_run else result
        results = _bulk_run(
            ids, lambda rid: _delete_one(c, entity, rid, group_id=group_id,
                                         object_type=object_type, dry_run=dry_run))
        failed = [{"index": i, "id": ids[i], "error": val} for i, ok, val in results if not ok]
        if dry_run:
            would_delete = [{"index": i, **val} for i, ok, val in results if ok]
            return {"dry_run": True, "total": len(ids), "would_delete": would_delete,
                    "failed": failed}
        return {"total": len(ids), "succeeded": len(ids) - len(failed), "failed": failed}

    @mcp.tool()
    def folk_add_to_group(
        entity: str,
        group_id: str,
        id: Optional[str] = None,
        ids: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Add one or several existing people/companies to one Folk group.

        The inverse of `folk_update`'s `add_to_groups` (which batches
        *groups* for *one* record) — this batches *records* into *one*
        group. Reads each record's current groups and writes back the union
        (Folk's `groups` field is replace-all on PATCH), so existing group
        membership is preserved. A record already in the group is a no-op
        success, not an error.

        Pass `id` for ONE record (returns the record/diff directly). Pass
        `ids` (up to 50) for several (returns a receipt). Exactly one of
        `id`/`ids` is required.

        Args:
            entity: "person" or "company".
            group_id: the target group (see folk_list_groups).
            id: record ID — solo mode.
            ids: record IDs — bulk mode.
            dry_run: si vrai, n'écrit rien — renvoie le diff `groups: {"from",
                "to"}` (déjà membre → from == to).

        Returns:
            id: the updated record, or {"dry_run": true, "id", "changes", ...}.
            ids: {"total", "succeeded", "failed": [{"index","id","error"}]},
                or dry_run: {"dry_run": true, "total", "would_add": [...], "failed": [...]}.
        """
        if (id is None) == (ids is None):
            raise _bad("fournir soit `id` (un seul record) soit `ids` (plusieurs) "
                       "— pas les deux, pas ni l'un ni l'autre.")
        if entity not in _GROUP_ENTITIES:
            raise _bad(f"entity doit être l'un de {_GROUP_ENTITIES}.")
        c = _client()
        if id is not None:
            result = _update_one(c, entity, id, add_to_groups=[group_id], dry_run=dry_run)
            return {"dry_run": True, **result} if dry_run else result
        results = _bulk_run(
            ids, lambda rid: _update_one(c, entity, rid, add_to_groups=[group_id],
                                         dry_run=dry_run))
        failed = [{"index": i, "id": ids[i], "error": val} for i, ok, val in results if not ok]
        if dry_run:
            would_add = [{"index": i, **val} for i, ok, val in results if ok]
            return {"dry_run": True, "total": len(ids), "would_add": would_add,
                    "failed": failed}
        return {"total": len(ids), "succeeded": len(ids) - len(failed), "failed": failed}

    # --- users (membres du workspace, lecture seule) ------------------------

    @mcp.tool()
    def folk_list_users() -> dict:
        """List the workspace users (members) — useful to resolve owners/assignees."""
        return {"users": _client().list_users()}

    @mcp.tool()
    def folk_get_user(user_id: str = "me") -> dict:
        """Fetch a workspace user by ID. `user_id="me"` (default) returns the
        authenticated user — call it to attribute an action to the current user."""
        return _client().get_user(user_id)

    # --- lists (énumération par collection) ---------------------------------

    @mcp.tool()
    def folk_list_deals(group_id: str, object_type: str = "deals") -> dict:
        """List the deals (or another custom object) of a Folk group.

        Args:
            group_id: Folk group ID (see folk_list_groups).
            object_type: Custom-object collection name (default "deals").
        """
        return {"deals": _client().list_deals(group_id, object_type=object_type)}

    @mcp.tool()
    def folk_list_notes(entity_id: Optional[str] = None) -> dict:
        """List notes, optionally filtered to one entity (person/company/deal ID)."""
        return {"notes": _client().list_notes(entity_id=entity_id)}

    @mcp.tool()
    def folk_list_reminders(entity_id: Optional[str] = None) -> dict:
        """List reminders, optionally filtered to one entity."""
        return {"reminders": _client().list_reminders(entity_id=entity_id)}
