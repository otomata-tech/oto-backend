"""Folk CRM — groups, people, companies, deals, notes, interactions, reminders.

Wrappe `oto.tools.folk.FolkClient` (API publique https://developer.folk.app).
Clé résolue par appel via `access.resolve_api_key("folk")` — provider byo-only
(user key posée sur /account, ou credential partagé de l'org active). Pas de
clé plateforme.

Surface : lecture/écriture **par entité** (`folk_search`/`get`/`update`/`delete`
prennent `entity` = person|company[|deal]) — fusion sans perte. Les **créations**
restent des outils typés (`folk_create_*`) : leurs champs guident le modèle.

**Bulk** (`folk_bulk_create`/`update`/`delete`/`add_to_group`, ≤50 items) :
Folk n'a d'endpoint batch nulle part — ces outils bouclent sur les méthodes
single-record ci-dessus (petit délai de courtoisie) et renvoient un reçu
allégé (compte + erreurs par item), pas N corps de réponse complets.
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
    def folk_update(
        entity: str, id: str, fields: Optional[dict] = None,
        group_id: Optional[str] = None, object_type: str = "deals",
        add_to_groups: Optional[list[str]] = None,
        remove_from_groups: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Update a Folk record (PATCH — only the given fields change).

        Args:
            entity: "person", "company" or "deal".
            id: the record ID (the deal_id for a deal).
            fields: Folk API field names, camelCase (e.g. {"jobTitle": "CTO"},
                {"industry": "SaaS"}, ou champs custom d'un deal). Optionnel si
                seuls `add_to_groups`/`remove_from_groups` sont fournis.
                **Champs CUSTOM d'une person/company** (ex. Status d'un groupe) :
                les passer SOUS `customFieldValues`, keyés par group_id —
                `{"customFieldValues": {"<group_id>": {"Status": "Follow-up"}}}`.
                Un champ custom passé à plat (`{"Status": …}`) est rejeté (422
                "Unrecognized key"). La structure se découvre via folk_search
                (customFieldValues groupée par group_id).
            group_id: REQUIRED for `deal` only (le groupe où vit le deal). Ne PAS
                le passer pour person/company (sans effet, source d'erreur) — pour
                leurs champs custom, utiliser `customFieldValues` ci-dessus.
            object_type: nom de la collection (défaut "deals"), `deal` seulement.
            add_to_groups / remove_from_groups: rattacher/détacher une **person** ou
                **company** À des groupes (folk_list_groups pour les IDs), sans
                toucher ses autres groupes. L'API Folk étant en *replace-all* sur
                `groups` (un PATCH direct écraserait les autres groupes), le serveur
                relit les groupes actuels et écrit l'union. Ne pas passer `groups`
                dans `fields` en même temps.
            dry_run: si vrai, ne PATCH pas — renvoie `{"changes": {field:
                {"from", "to"}}}` (valeur actuelle relue vs proposée) pour relire
                avant d'écrire. Pour `entity="note"` (pas de get-par-id côté
                Folk), renvoie `{"fields": ..., "current_available": False}` —
                aperçu des champs envoyés, sans le "from".
        """
        if entity not in ("person", "company", "deal"):
            raise _bad("entity doit être 'person', 'company' ou 'deal'.")
        result = _update_one(
            _client(), entity, id, fields=fields, group_id=group_id,
            object_type=object_type, add_to_groups=add_to_groups,
            remove_from_groups=remove_from_groups, dry_run=dry_run)
        return {"dry_run": True, **result} if dry_run else result

    @mcp.tool()
    def folk_delete(
        entity: str, id: str,
        group_id: Optional[str] = None, object_type: str = "deals",
        dry_run: bool = False,
    ) -> dict:
        """Delete a Folk record. Irreversible.

        Args:
            entity: "person", "company" or "deal".
            id: the record ID (the deal_id for a deal).
            group_id: REQUIRED for `deal` only (the group where the deal lives).
            object_type: collection name (default "deals"), `deal` only.
            dry_run: si vrai, ne supprime PAS — renvoie `{"would_delete": <record
                actuel>}` pour relire avant de détruire. Pour `entity="note"`
                (pas de get-par-id côté Folk), `would_delete` est `None` +
                `"current_available": False`.
        """
        if entity not in ("person", "company", "deal"):
            raise _bad("entity doit être 'person', 'company' ou 'deal'.")
        result = _delete_one(_client(), entity, id, group_id=group_id,
                             object_type=object_type, dry_run=dry_run)
        return {"dry_run": True, **result} if dry_run else result

    # --- bulk (boucle serveur, pas un endpoint batch natif Folk) -------------
    #
    # Folk n'a d'endpoint batch nulle part (vérifié sur les 3 surfaces Folk
    # disponibles : ce connecteur, le MCP officiel Folk, et un MCP tiers). Ces
    # 4 outils bouclent sur les méthodes single-record ci-dessus avec un délai
    # de courtoisie (`_bulk_run`) et renvoient un reçu allégé plutôt que N
    # corps de réponse complets.

    @mcp.tool()
    def folk_bulk_create(
        entity: str, items: list[dict],
        group_id: Optional[str] = None, object_type: str = "deals",
        dry_run: bool = False,
    ) -> dict:
        """Create up to 50 Folk records of the same entity type in one call.

        Loops `folk_create_*` per item (small delay between calls — Folk has
        no native batch-create endpoint). A rejected item does not abort the
        batch; the receipt lists which succeeded (with their new IDs, needed
        to link/group/enrich them next) and which failed (with why).

        Args:
            entity: "person", "company", "deal", "note", "interaction" or "reminder".
            items: one dict per record, same fields as the matching
                `folk_create_*` tool (e.g. for "person":
                {"first_name": ..., "company_id": ..., "group_ids": [...]}).
            group_id: REQUIRED for `entity="deal"` only — all items land in
                this one group (Folk deals aren't creatable across groups in
                a single call).
            object_type: collection name (default "deals"), `deal` only.
            dry_run: si vrai, ne crée RIEN — renvoie `would_create` (les champs
                résolus par item) au lieu de `created`, pour relire avant
                d'écrire. Aucun appel réseau dans ce mode.

        Returns:
            {"total": N, "succeeded": K, "created": [{"index", "id"}, ...],
             "failed": [{"index", "error"}, ...]}
            (dry_run: "created" devient "would_create": [{"index",
             "would_create", ...}, ...], + "dry_run": true)
        """
        if entity not in _CREATE_ENTITIES:
            raise _bad(f"entity doit être l'un de {_CREATE_ENTITIES}.")
        if entity == "deal" and not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        c = _client()
        results = _bulk_run(
            items, lambda item: _create_one(c, entity, group_id=group_id,
                                            object_type=object_type,
                                            dry_run=dry_run, **item))
        failed = [{"index": i, "error": val} for i, ok, val in results if not ok]
        if dry_run:
            would_create = [{"index": i, **val} for i, ok, val in results if ok]
            return {"dry_run": True, "total": len(items), "would_create": would_create,
                    "failed": failed}
        created = [{"index": i, "id": val.get("id")} for i, ok, val in results if ok]
        return {"total": len(items), "succeeded": len(created),
                "created": created, "failed": failed}

    @mcp.tool()
    def folk_bulk_update(
        entity: str, items: list[dict],
        group_id: Optional[str] = None, object_type: str = "deals",
        dry_run: bool = False,
    ) -> dict:
        """Update up to 50 Folk records of the same entity type in one call.

        Loops `folk_update` per item — same semantics (PATCH, only given
        fields change; `groups` field-list merge for add_to_groups/
        remove_from_groups). A rejected item does not abort the batch.

        Args:
            entity: "person", "company", "deal", "note" or "reminder"
                (interactions have no update endpoint in Folk).
            items: one dict per record: {"id": ..., "fields": {...},
                "add_to_groups": [...], "remove_from_groups": [...]}
                (`fields` and/or the group lists — see `folk_update` for the
                field vocabulary per entity, e.g. `customFieldValues`).
            group_id: REQUIRED for `entity="deal"` only (the group all these
                deals live in).
            object_type: collection name (default "deals"), `deal` only.
            dry_run: si vrai, ne PATCH rien — relit chaque record et renvoie
                `would_update` avec un diff `{"changes": {field: {"from",
                "to"}}}` par item, pour relire avant d'écrire (utile en
                particulier ici : contrairement à create/delete, un mauvais
                bulk_update écrase une donnée existante sans trace). Pour un
                item `entity="note"` (pas de get-par-id côté Folk),
                `"current_available": False` — aperçu des champs envoyés sans
                le "from".

        Returns:
            {"total": N, "succeeded": K, "failed": [{"index", "id", "error"}, ...]}
            (dry_run: "succeeded"/pas d'écriture devient "would_update":
             [{"index", "id", "changes"|"fields", ...}, ...], + "dry_run": true)
        """
        if entity not in _UPDATE_ENTITIES:
            raise _bad(f"entity doit être l'un de {_UPDATE_ENTITIES}.")
        if entity == "deal" and not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        c = _client()

        def _one(item):
            if "id" not in item:
                raise _bad("chaque item doit contenir 'id'.")
            return _update_one(
                c, entity, item["id"], fields=item.get("fields"),
                group_id=group_id, object_type=object_type,
                add_to_groups=item.get("add_to_groups"),
                remove_from_groups=item.get("remove_from_groups"),
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
    def folk_bulk_delete(
        entity: str, ids: list[str],
        group_id: Optional[str] = None, object_type: str = "deals",
        dry_run: bool = False,
    ) -> dict:
        """Delete up to 50 Folk records of the same entity type in one call.
        Irreversible.

        Args:
            entity: "person", "company", "deal", "note" or "reminder"
                (interactions have no delete endpoint in Folk).
            ids: record IDs (deal IDs for entity="deal").
            group_id: REQUIRED for `entity="deal"` only.
            object_type: collection name (default "deals"), `deal` only.
            dry_run: si vrai, ne supprime RIEN — relit chaque record et
                renvoie `would_delete` (le record actuel par item), pour
                vérifier ce qui serait détruit avant de le faire. Pour
                `entity="note"` (pas de get-par-id côté Folk), le record est
                `None` + `"current_available": False`.

        Returns:
            {"total": N, "succeeded": K, "failed": [{"index", "id", "error"}, ...]}
            (dry_run: "succeeded" devient "would_delete": [{"index", "id",
             "would_delete", ...}, ...], + "dry_run": true)
        """
        if entity not in _DELETE_ENTITIES:
            raise _bad(f"entity doit être l'un de {_DELETE_ENTITIES}.")
        if entity == "deal" and not group_id:
            raise _bad("group_id requis pour entity='deal'.")
        c = _client()
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
    def folk_bulk_add_to_group(
        entity: str, ids: list[str], group_id: str, dry_run: bool = False,
    ) -> dict:
        """Add up to 50 existing people/companies to one Folk group in one call.

        The inverse of `folk_update`'s `add_to_groups` (which batches *groups*
        for *one* record) — this batches *records* into *one* group. Reads
        each record's current groups and writes back the union (Folk's
        `groups` field is replace-all on PATCH), so existing group membership
        is preserved. A record already in the group is a no-op success, not
        an error.

        Args:
            entity: "person" or "company".
            ids: record IDs to add to the group.
            group_id: the target group (see folk_list_groups).
            dry_run: si vrai, n'écrit rien — renvoie `would_add` avec le diff
                `groups: {"from", "to"}` par item (déjà membre → from == to).

        Returns:
            {"total": N, "succeeded": K, "failed": [{"index", "id", "error"}, ...]}
            (dry_run: "succeeded" devient "would_add": [{"index", "id",
             "changes", ...}, ...], + "dry_run": true)
        """
        if entity not in _GROUP_ENTITIES:
            raise _bad(f"entity doit être l'un de {_GROUP_ENTITIES}.")
        c = _client()
        results = _bulk_run(
            ids, lambda rid: _update_one(c, entity, rid, add_to_groups=[group_id],
                                         dry_run=dry_run))
        failed = [{"index": i, "id": ids[i], "error": val} for i, ok, val in results if not ok]
        if dry_run:
            would_add = [{"index": i, **val} for i, ok, val in results if ok]
            return {"dry_run": True, "total": len(ids), "would_add": would_add,
                    "failed": failed}
        return {"total": len(ids), "succeeded": len(ids) - len(failed), "failed": failed}

    # --- créations (typées : les champs guident le modèle) ------------------

    @mcp.tool()
    def folk_create_person(
        first_name: str,
        last_name: Optional[str] = None,
        emails: Optional[list[str]] = None,
        phones: Optional[list[str]] = None,
        job_title: Optional[str] = None,
        company_name: Optional[str] = None,
        company_id: Optional[str] = None,
        group_ids: Optional[list[str]] = None,
        urls: Optional[list[str]] = None,
        description: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Create a person in Folk.

        Args:
            first_name: Required.
            company_id: Link to an existing company (takes precedence over company_name).
            company_name: Create/match a company by name.
            group_ids: Groups to add the person to (see folk_list_groups — groups
                are not creatable via API, only in the Folk app).
            urls: Associated URLs (the first is the primary). Put the LinkedIn
                profile URL here for a contact sourced via Unipile.
            description: Free-text description on the person record. For a richer
                or status note, use folk_create_note after creation.
            dry_run: si vrai, ne crée rien — renvoie `{"would_create": {...}}`
                (les champs résolus) pour relire avant d'écrire.
        """
        result = _create_one(
            _client(), "person", dry_run=dry_run,
            first_name=first_name, last_name=last_name, emails=emails, phones=phones,
            job_title=job_title, company_name=company_name, company_id=company_id,
            group_ids=group_ids, urls=urls, description=description,
        )
        return {"dry_run": True, **result} if dry_run else result

    @mcp.tool()
    def folk_create_company(
        name: str,
        emails: Optional[list[str]] = None,
        industry: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Create a company in Folk.

        Args:
            dry_run: si vrai, ne crée rien — renvoie `{"would_create": {...}}`.
        """
        result = _create_one(_client(), "company", dry_run=dry_run,
                             name=name, emails=emails, industry=industry)
        return {"dry_run": True, **result} if dry_run else result

    @mcp.tool()
    def folk_create_deal(
        group_id: str,
        name: str,
        object_type: str = "deals",
        people_ids: Optional[list[str]] = None,
        company_ids: Optional[list[str]] = None,
        custom_fields: Optional[dict] = None,
        dry_run: bool = False,
    ) -> dict:
        """Create a deal in a Folk group, optionally linked to people/companies.

        Args:
            custom_fields: Custom field values keyed by field name (e.g. status, amount).
            dry_run: si vrai, ne crée rien — renvoie `{"would_create": {...}}`.
        """
        result = _create_one(
            _client(), "deal", group_id=group_id, object_type=object_type,
            dry_run=dry_run, name=name, people_ids=people_ids,
            company_ids=company_ids, custom_fields=custom_fields,
        )
        return {"dry_run": True, **result} if dry_run else result

    @mcp.tool()
    def folk_create_note(
        entity_id: str, content: str, visibility: str = "public",
        dry_run: bool = False,
    ) -> dict:
        """Attach a note to a Folk entity (person/company/deal).

        Args:
            visibility: "public" (whole workspace) or "private".
            dry_run: si vrai, ne crée rien — renvoie `{"would_create": {...}}`.
        """
        result = _create_one(_client(), "note", dry_run=dry_run, entity_id=entity_id,
                             content=content, visibility=visibility)
        return {"dry_run": True, **result} if dry_run else result

    @mcp.tool()
    def folk_update_note(
        note_id: str,
        content: Optional[str] = None,
        visibility: Optional[str] = None,
    ) -> dict:
        """Edit an existing Folk note (PATCH — only the given fields change).

        Args:
            note_id: the note ID (nte_…).
            content: new note content (markdown).
            visibility: "public" (whole workspace) or "private".
        """
        fields: dict = {}
        if content is not None:
            fields["content"] = content
        if visibility is not None:
            fields["visibility"] = visibility
        if not fields:
            raise _bad("rien à mettre à jour : passe content et/ou visibility.")
        return _client().update_note(note_id, **fields)

    @mcp.tool()
    def folk_delete_note(note_id: str) -> dict:
        """Delete a Folk note. Irreversible. `note_id` = the note ID (nte_…)."""
        return _client().delete_note(note_id)

    @mcp.tool()
    def folk_create_interaction(
        entity_id: str,
        type: str,
        title: str,
        content: Optional[str] = None,
        date_time: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Log an interaction (call, meeting, email…) on a Folk entity.

        Args:
            type: Interaction type as defined in the workspace (e.g. "call", "meeting").
            date_time: ISO 8601 (defaults to now server-side).
            dry_run: si vrai, ne crée rien — renvoie `{"would_create": {...}}`.
        """
        result = _create_one(
            _client(), "interaction", dry_run=dry_run, entity_id=entity_id,
            type=type, title=title, content=content, date_time=date_time,
        )
        return {"dry_run": True, **result} if dry_run else result

    @mcp.tool()
    def folk_create_reminder(
        entity_id: str, name: str, recurrence_rule: str, visibility: str = "public",
        dry_run: bool = False,
    ) -> dict:
        """Create a reminder on a Folk entity.

        Args:
            recurrence_rule: iCal RRULE (e.g. "DTSTART:20260701T090000Z\\nRRULE:FREQ=WEEKLY").
            dry_run: si vrai, ne crée rien — renvoie `{"would_create": {...}}`.
        """
        result = _create_one(
            _client(), "reminder", dry_run=dry_run, entity_id=entity_id, name=name,
            recurrence_rule=recurrence_rule, visibility=visibility,
        )
        return {"dry_run": True, **result} if dry_run else result

    @mcp.tool()
    def folk_get_reminder(reminder_id: str) -> dict:
        """Fetch a Folk reminder by ID (full record). `reminder_id` = rmd_…."""
        return _client().get_reminder(reminder_id)

    @mcp.tool()
    def folk_update_reminder(
        reminder_id: str,
        name: Optional[str] = None,
        recurrence_rule: Optional[str] = None,
        visibility: Optional[str] = None,
    ) -> dict:
        """Edit an existing Folk reminder (PATCH — only the given fields change).

        Args:
            reminder_id: the reminder ID (rmd_…).
            recurrence_rule: iCal RRULE (e.g. "DTSTART:20260701T090000Z\\nRRULE:FREQ=WEEKLY").
            visibility: "public" (whole workspace) or "private".
        """
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if recurrence_rule is not None:
            fields["recurrenceRule"] = recurrence_rule
        if visibility is not None:
            fields["visibility"] = visibility
        if not fields:
            raise _bad("rien à mettre à jour : passe name, recurrence_rule et/ou visibility.")
        return _client().update_reminder(reminder_id, **fields)

    @mcp.tool()
    def folk_delete_reminder(reminder_id: str) -> dict:
        """Delete a Folk reminder. Irreversible. `reminder_id` = rmd_…."""
        return _client().delete_reminder(reminder_id)

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
