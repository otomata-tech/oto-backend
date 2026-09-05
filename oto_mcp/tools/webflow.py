"""Webflow — CMS (site/collections/items) + webhooks, API v2
(developers.webflow.com/data).

Wrappe `oto.tools.webflow.client.WebflowClient`. Credential = clé unique
(`keyed=True`, `secret_kind="api_key"`, `access.resolve_api_key("webflow")`) :
un Site API token Webflow est bound à UN site (vérifié contre
`reference/authentication/site-token` — « Site tokens are created per site »),
donc AUCUN `site_id` à saisir ni à faire voyager ici — le client (oto-core)
le résout lui-même via `GET /sites` au premier appel qui en a besoin, mis en
cache pour la durée de vie du client. byo-only (pas de clé plateforme).

⚠️ **Un item créé/modifié ici reste invisible sur le site public tant que
`webflow_publish` n'a pas été appelé** — le seul tool de ce module qui touche
le contenu LIVE, laissé SEUL (comme `cognism_redeem` face à `cognism_search` :
la frontière entre « rien ne bouge » et « ça devient public » doit rester
visible dans le nom du tool, pas noyée dans un `op` parmi d'autres). Tout le
reste — site/collections/items — est UN SEUL tool consolidé, `webflow_cms`,
verbe en `op` (convention Folk/Cognism ADR 0047 §Amendement) : côté agent
comme côté catalogue dashboard (qui liste les outils PAR connecteur sous une
même carte), le CMS se présente comme une chose, pas quatre. create/update
valident `fieldData` contre le schéma réel de la collection
(`webflow_cms(op="collection")`) avant tout appel réseau d'écriture : un slug
inconnu ou un champ requis manquant est nommé dans l'erreur plutôt que de
laisser filer un 400 Webflow opaque à l'agent.

`webflow_webhooks` (list/get/create/delete) est la surface RÉELLE de l'API —
**aucun endpoint update n'existe** (vérifié live 2026-08-20 : reconfigurer un
webhook = delete + create). `secretKey` n'est renvoyé QU'À LA CRÉATION (jamais
sur get/list, confirmé live) — le docstring de `op="create"` le signale, comme
Folk pour `signingSecret`.

`webflow_forms` (list/get — schéma des formulaires, jamais de write : un
formulaire se construit dans l'éditeur visuel Webflow, pas par API) et
`webflow_submissions` (list/get/update/delete — les LEADS remplis par de
vrais visiteurs) restent DEUX tools distincts : la forme des paramètres ne se
recouvre pas (`form_id` seul vs `submission_id` + `form_id` optionnel selon
l'op), et fusionner masquerait la frontière « catalogue de formulaires » vs
« données de contact réelles ». ⚠️ **Aucune création par API** — une
soumission n'existe que si un visiteur remplit le formulaire côté site
public ; `op="update"` sur `webflow_submissions` ne touche QUE les hidden
fields déclarés au schéma du formulaire, jamais les données soumises
(non éditables après coup côté Webflow).

`webflow_pages` (op=list|get|update|content) couvre les MÉTADONNÉES de page
(title/slug/seo/openGraph — read+write, sans restriction) et la LECTURE du
contenu statique (les text nodes — titres/paragraphes). ⚠️ **L'ÉCRITURE du
contenu n'est PAS exposée ici** : l'API Webflow ne permet d'écrire le
contenu statique d'une page QUE sur une locale SECONDAIRE du site (jamais la
primaire — confirmé verbatim contre la doc source), donc un site
mono-locale (le cas courant, dont celui utilisé pour tester ce connecteur)
n'a structurellement aucun moyen d'éditer le corps d'une page via cette API
— ajouter un `op="update_content"` qui échoue systématiquement sur la
majorité des sites serait un piège, pas une fonctionnalité. `webflow_
site_publish` (distinct de `webflow_publish`, qui ne publie QUE des items
CMS) publie le SITE ENTIER — rate-limité par Webflow à 1/minute, laissé
SEUL comme les autres tools qui rendent du contenu public.

Volontairement HORS PÉRIMÈTRE (v1) : assets, ecommerce, comments, custom
code — ce dernier injecte du JS arbitraire exécuté par CHAQUE visiteur
(site ou page entière), un risque catégoriquement différent d'un CRUD de
données ; à construire seulement sur un besoin concret, avec des garde-fous
dédiés (jamais un simple `op` de plus parmi d'autres).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from oto.tools.common.errors import UpstreamHTTPError

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /v2/token/authorized_by`. Ce que la doc Webflow établit :

    - **authentifié** — Bearer token, comme le reste de l'API ;
    - **sans effet de bord** — une lecture d'identité (`id`, `email`,
      `firstName`, `lastName`) ;
    - **le coût** — aucune mention de coût ni de limite de débit particulière
      pour cet appel. Absence de mention, indice, pas une preuve.

    ⚠️ **Quatrième règle d'oto#69 : une sonde ne transforme jamais sa propre
    limite en verdict sur la clé.** Cet endpoint exige le scope
    `authorized_user:read` — SÉPARÉ des scopes réels du connecteur
    (`cms:read`/`sites:read`). Un jeton légitimement scopé pour le CMS peut
    refuser CET appel sans être cassé. Webflow documente 401 pour un jeton
    mort et 403 pour un scope manquant (`UpstreamHTTPError.status_code`,
    jamais deviné sur le texte) : le 403 lève un `RuntimeError` NU (jamais
    `NonAutorise`) pour tomber sur `unknown`, pas `unauthorized` — un faux
    négatif ici pousserait à révoquer une clé qui marche sur son CMS.
    """
    from oto.tools.webflow.client import WebflowClient

    try:
        infos = WebflowClient(api_key=fields["token"])._request(
            "GET", "token/authorized_by") or {}
    except UpstreamHTTPError as e:
        if e.status_code == 403:
            raise RuntimeError(
                "Webflow refuse CET appel de vérification (403, scope "
                "authorized_user:read) — ça ne dit RIEN de la clé pour "
                "l'usage réel du connecteur (CMS, un scope différent). Non "
                "concluant, pas invalide.") from e
        if e.status_code == 401:
            raise connector_verify.NonAutorise(
                f"Webflow refuse cette clé (401) : {str(e)[:200]}") from e
        raise
    if not infos.get("id"):
        raise RuntimeError(
            "Webflow a répondu sans identifier d'utilisateur pour cette clé — "
            f"réponse inattendue : {str(infos)[:200]}")

_BULK_MAX_ITEMS = 50

_WebhookTriggerType = Literal[
    "form_submission", "site_publish",
    "page_created", "page_metadata_updated", "page_deleted",
    "ecomm_new_order", "ecomm_order_changed", "ecomm_inventory_changed",
    "collection_item_created", "collection_item_changed",
    "collection_item_deleted", "collection_item_published",
    "collection_item_unpublished", "comment_created",
]


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    if value is None:
        raise _bad(f"op='{op}' requiert {name}")
    return value


def _run(fn):
    """Exécute un appel Webflow, traduit une erreur amont en McpError actionnable.

    `ValueError` = le client a résolu `site_id` via `GET /sites` et vu 0 ou
    >1 site (token de workspace passé par erreur, scope `sites:read` absent,
    token révoqué) — pas un refus HTTP, mais tout aussi actionnable pour
    l'appelant."""
    try:
        return fn()
    except McpError:
        raise
    except ValueError as e:
        raise _bad(str(e))
    except UpstreamHTTPError as e:
        if e.status_code == 401:
            msg = "Token Webflow invalide ou révoqué (401). Vérifie le token posé."
        elif e.status_code == 404:
            msg = f"Webflow : ressource introuvable (404) — {e.body}"
        elif e.status_code >= 500:
            msg = (f"Webflow est momentanément indisponible (erreur serveur "
                   f"{e.status_code}). Réessaie dans un moment — ce n'est pas "
                   "ton entrée.")
        else:
            msg = f"Webflow a refusé la requête (HTTP {e.status_code}) : {e.body}"
        raise _bad(msg)


def _known_field_slugs(collection: dict) -> set:
    return {"name", "slug"} | {
        f.get("slug") for f in collection.get("fields", []) if f.get("slug")
    }


def _required_field_slugs(collection: dict) -> set:
    return {"name", "slug"} | {
        f.get("slug") for f in collection.get("fields", [])
        if f.get("slug") and f.get("isRequired")
    }


def _validate_field_data(collection: dict, field_data: dict, *, op: str,
                          check_required: bool) -> None:
    """Refuse un `fieldData` AVANT tout appel réseau d'écriture : un slug
    inconnu ou (`check_required`) un champ requis absent nomme le(s) coupable(s)
    dans l'erreur, plutôt que de laisser Webflow renvoyer un 400 générique que
    l'agent ne peut pas exploiter."""
    known = _known_field_slugs(collection)
    unknown = set(field_data) - known
    if unknown:
        raise _bad(
            f"webflow_cms(op='{op}') : champ(s) inconnu(s) dans fieldData "
            f"pour cette collection : {sorted(unknown)}. Champs disponibles : "
            f"{sorted(known)}.")
    if check_required:
        missing = _required_field_slugs(collection) - set(field_data)
        if missing:
            raise _bad(
                f"webflow_cms(op='{op}') : champ(s) requis manquant(s) dans "
                f"fieldData : {sorted(missing)}.")


def register(mcp: FastMCP) -> None:
    from oto.tools.webflow.client import WebflowClient

    connector_verify.register("webflow", _verify)

    def _client() -> WebflowClient:
        key, _ = access.resolve_api_key("webflow")
        return WebflowClient(api_key=key)

    @mcp.tool()
    def webflow_cms(
        op: Literal["site", "collections", "collection", "items", "item",
                    "create", "update", "delete"],
        collection_id: Optional[str] = None,
        id: Optional[str] = None,
        ids: Optional[list[str]] = None,
        item: Optional[dict] = None,
        items: Optional[list[dict]] = None,
        offset: int = 0,
        max_results: int = 100,
        sort_by: Optional[Literal["createdOn", "lastPublished", "lastUpdated",
                                   "name", "slug"]] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        cms_locale_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Webflow CMS — the site, its collections, and their items. This is
        THE tool for "what's my Webflow site", "list my CMS collections", and
        "find/add/update/delete an item" (a blog post, a product, any CMS
        record) — Webflow has no separate `webflow_site`/`webflow_collections`
        tool, `op` picks the target+verb. Items created/updated here are
        STAGED (draft): nothing is visible on the live site until
        `webflow_publish`.

        `op`:
        - **"site"**: the site pinned to this connector's credential (a
          Webflow Site API token is bound to exactly one site — no `site_id`
          param, this always returns THAT site: id, displayName, shortName,
          customDomains, lastPublished, timeZone...).
        - **"collections"**: list all CMS collections of the site (id,
          displayName, slug — no field schema, use op="collection" for that).
        - **"collection"**: one collection's full schema (`collection_id`),
          including `fields[]` (each field's `slug`, `displayName`, `type`,
          `isRequired`) — the slugs op="create"/"update" need for `fieldData`.
        - **"items"**: one paginated page of a collection's items
          (`collection_id` + `offset`/`max_results`, capped at 500),
          optionally sorted.
        - **"item"**: one item by `id` (+ `collection_id`).
        - **"create"**: one (`item`) or several (`items`, ≤50) new items in
          `collection_id`. `fieldData` keys are validated against the
          collection's schema (op="collection") before any write — an unknown
          slug or a missing required field is refused first.
        - **"update"**: PATCH one (`id` + `item`) or several (`items`, ≤50 —
          each needs an `"id"` key) items in `collection_id`.
        - **"delete"**: delete one (`id`) or several (`ids`, ≤50) items from
          `collection_id`.

        Solo vs bulk: exactly one of `item`/`items` (create), `id`/`items`
        (update), `id`/`ids` (delete) is required. Webflow has a REAL batch
        endpoint (all items in one HTTP call) — bulk here is one request, not
        a client-side loop.

        Returns —
            site: the site.
            collections: `{"collections": [...]}`.
            collection: the collection schema.
            items: `{"items": [...], "pagination": {"total", "offset", "limit"}}`.
            item: the item.
            create solo: the created item, or `{"dry_run": true, "would_create"}`.
            create bulk: `{"total", "succeeded", "created": [{"index","id"}],
                "failed": []}`, or dry_run preview.
            update solo: the updated item, or `{"dry_run": true, "id", "changes"}`.
            update bulk: `{"total", "succeeded", "failed": []}`, or dry_run preview.
            delete solo: `{}`, or `{"dry_run": true, "id", "would_delete"}`.
            delete bulk: `{"total", "succeeded", "failed": []}`, or dry_run preview.

        Args:
            op: site | collections | collection | items | item | create |
                update | delete.
            collection_id: required for every op except "site".
            id: item id — op="item", solo update/delete.
            ids: item ids — bulk delete.
            item: op="create" solo — `{"fieldData": {...}, "isArchived"?: bool,
                "isDraft"?: bool}`. op="update" solo (with `id` set separately)
                — same shape, `fieldData` optional (only given keys change).
            items: op="create" bulk — list of the create shape above.
                op="update" bulk — list of `{"id", "fieldData"?, "isArchived"?,
                "isDraft"?}`.
            offset, max_results: op="items" pagination — max_results capped at
                500 server-side.
            sort_by, sort_order: op="items" — sort the page.
            cms_locale_id: op="items"/"item" — locale for multi-locale
                collections.
            dry_run: create — validates `fieldData` against the schema (one
                read call), makes NO create call, returns `would_create`.
                update/delete — fetches the item(s) first and returns a real
                diff (`changes: {field: {"from", "to"}}`) or `would_delete`
                (the current record) — never an echo of the input.
        """
        c = _client()

        if op == "site":
            return _run(lambda: c.get_site())

        if op == "collections":
            return {"collections": _run(lambda: c.list_collections())}

        if op == "collection":
            _need(collection_id, "collection_id", op)
            return _run(lambda: c.get_collection(collection_id))

        _need(collection_id, "collection_id", op)

        if op == "items":
            return _run(lambda: c.list_items(
                collection_id, offset=offset, limit=min(max_results, 500),
                sort_by=sort_by, sort_order=sort_order,
                cms_locale_id=cms_locale_id))

        if op == "item":
            _need(id, "id", op)
            return _run(lambda: c.get_item(collection_id, id))

        if op == "create":
            if (item is None) == (items is None):
                raise _bad("op='create' : fournir soit `item` soit `items` — "
                           "pas les deux, pas ni l'un ni l'autre.")
            payload = [item] if item is not None else list(items)
            if len(payload) > _BULK_MAX_ITEMS:
                raise _bad(f"trop d'éléments ({len(payload)}) — max "
                           f"{_BULK_MAX_ITEMS} par appel.")
            collection = _run(lambda: c.get_collection(collection_id))
            for it in payload:
                _validate_field_data(collection, it.get("fieldData") or {},
                                     op=op, check_required=True)
            if dry_run:
                if item is not None:
                    return {"dry_run": True, "would_create": payload[0]}
                return {"dry_run": True, "total": len(payload),
                        "would_create": payload}
            result = _run(lambda: c.create_items(collection_id, payload))
            created = result.get("items", [])
            if item is not None:
                return created[0] if created else {}
            return {"total": len(payload), "succeeded": len(created),
                    "created": [{"index": i, "id": it.get("id")}
                               for i, it in enumerate(created)],
                    "failed": []}

        if op == "update":
            if (id is None) == (items is None):
                raise _bad("op='update' : fournir soit `id` (+ `item`) pour UN "
                           "item, soit `items` pour plusieurs — pas les deux, "
                           "pas ni l'un ni l'autre.")

            def _diff(current: dict, changed: dict) -> dict:
                changes = {}
                for k, v in changed.items():
                    if k in ("id",):
                        continue
                    if k == "fieldData":
                        for fk, fv in (v or {}).items():
                            changes[fk] = {
                                "from": (current.get("fieldData") or {}).get(fk),
                                "to": fv}
                    else:
                        changes[k] = {"from": current.get(k), "to": v}
                return changes

            if id is not None:
                field_data = (item or {}).get("fieldData")
                if field_data:
                    collection = _run(lambda: c.get_collection(collection_id))
                    _validate_field_data(collection, field_data, op=op,
                                         check_required=False)
                if dry_run:
                    current = _run(lambda: c.get_item(collection_id, id))
                    return {"dry_run": True, "id": id,
                            "changes": _diff(current, item or {})}
                payload = {"id": id, **(item or {})}
                result = _run(lambda: c.update_items(collection_id, [payload]))
                updated = result.get("items", [])
                return updated[0] if updated else {}

            if len(items) > _BULK_MAX_ITEMS:
                raise _bad(f"trop d'éléments ({len(items)}) — max "
                           f"{_BULK_MAX_ITEMS} par appel.")
            for it in items:
                if "id" not in it:
                    raise _bad("chaque item doit contenir 'id'.")
            needs_schema = any(it.get("fieldData") for it in items)
            if needs_schema:
                collection = _run(lambda: c.get_collection(collection_id))
                for it in items:
                    if it.get("fieldData"):
                        _validate_field_data(collection, it["fieldData"], op=op,
                                             check_required=False)
            if dry_run:
                would_update = []
                for it in items:
                    current = _run(lambda it=it: c.get_item(collection_id, it["id"]))
                    would_update.append({"id": it["id"], "changes": _diff(current, it)})
                return {"dry_run": True, "total": len(items),
                        "would_update": would_update}
            result = _run(lambda: c.update_items(collection_id, items))
            updated = result.get("items", [])
            return {"total": len(items), "succeeded": len(updated), "failed": []}

        if op == "delete":
            if (id is None) == (ids is None):
                raise _bad("op='delete' : fournir soit `id` soit `ids` — pas "
                           "les deux, pas ni l'un ni l'autre.")
            target_ids = [id] if id is not None else list(ids)
            if len(target_ids) > _BULK_MAX_ITEMS:
                raise _bad(f"trop d'éléments ({len(target_ids)}) — max "
                           f"{_BULK_MAX_ITEMS} par appel.")
            if dry_run:
                would_delete = [_run(lambda tid=tid: c.get_item(collection_id, tid))
                                for tid in target_ids]
                if id is not None:
                    return {"dry_run": True, "id": id,
                            "would_delete": would_delete[0]}
                return {"dry_run": True, "total": len(target_ids),
                        "would_delete": would_delete}
            _run(lambda: c.delete_items(collection_id, target_ids))
            if id is not None:
                return {}
            return {"total": len(target_ids), "succeeded": len(target_ids),
                    "failed": []}

        raise _bad("op doit être 'site', 'collections', 'collection', "
                   "'items', 'item', 'create', 'update' ou 'delete'.")

    @mcp.tool()
    def webflow_publish(
        collection_id: str,
        id: Optional[str] = None,
        ids: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Publish staged (draft) CMS items to the LIVE site — the only tool in
        this connector that makes content publicly visible (Webflow).

        Args:
            collection_id: the collection (see `webflow_cms(op="collections")`).
            id: one item id, OR...
            ids: several item ids (≤50).
            dry_run: resolves the item(s) and returns their CURRENT
                `isDraft`/`lastPublished` state (what would go live) — not an
                echo of the ids you passed. No publish call is made.
        """
        if (id is None) == (ids is None):
            raise _bad("fournir soit `id` soit `ids` — pas les deux, pas ni "
                       "l'un ni l'autre.")
        target_ids = [id] if id is not None else list(ids)
        if len(target_ids) > _BULK_MAX_ITEMS:
            raise _bad(f"trop d'éléments ({len(target_ids)}) — max "
                       f"{_BULK_MAX_ITEMS} par appel.")
        c = _client()
        if dry_run:
            would_publish = []
            for tid in target_ids:
                current = _run(lambda tid=tid: c.get_item(collection_id, tid))
                would_publish.append({
                    "id": tid, "isDraft": current.get("isDraft"),
                    "lastPublished": current.get("lastPublished"),
                })
            if id is not None:
                return {"dry_run": True, "would_publish": would_publish[0]}
            return {"dry_run": True, "total": len(target_ids),
                    "would_publish": would_publish}
        return _run(lambda: c.publish_items(collection_id, target_ids))

    @mcp.tool()
    def webflow_webhooks(
        op: Literal["list", "get", "create", "delete"] = "list",
        webhook_id: Optional[str] = None,
        trigger_type: Optional[_WebhookTriggerType] = None,
        url: Optional[str] = None,
        filter: Optional[dict] = None,
        dry_run: bool = False,
    ) -> dict:
        """Webflow webhooks on the pinned site — list, inspect, create, delete.
        Webflow POSTs an event payload to `url` each time `trigger_type` fires
        (site publish, a CMS item created/changed/deleted/published, a form
        submission, an ecommerce event...).

        ⚠️ **No update endpoint exists** (confirmed against Webflow's actual
        API, not just the docs) — to change a webhook's trigger/url/filter,
        delete it and create a new one.

        `op`:
        - **"list"** (default): every webhook registered on this site.
        - **"get"**: one webhook by `webhook_id`.
        - **"create"**: register a new webhook (`trigger_type` + `url`,
          optional `filter`). The response's `secretKey` (HMAC signing key for
          `x-webflow-signature`) is returned IN FULL only here — Webflow never
          shows it again on get/list, save it now if you need to verify
          payload signatures.
        - **"delete"**: unregister `webhook_id`. Irreversible — the webhook
          stops firing immediately.

        Returns —
            list: `{"webhooks": [...]}`.
            get: the webhook.
            create: the webhook INCLUDING `secretKey`, or
                `{"dry_run": true, "would_create"}`.
            delete: `{}`, or `{"dry_run": true, "webhook_id", "would_delete"}`.

        Args:
            op: list (default) | get | create | delete.
            webhook_id: op="get"/"delete".
            trigger_type: op="create" — the event that fires this webhook.
            url: op="create" — the HTTPS endpoint Webflow POSTs the event to.
            filter: op="create" — ONLY valid for trigger_type="form_submission"
                (`{"name": "<form name>"}`, scope to one form). Any other
                trigger_type + a filter is refused before the network call
                (Webflow itself 400s on this combination).
            dry_run: create — makes NO create call, returns `would_create`
                (no secretKey to preview — it doesn't exist until Webflow
                mints it). delete — fetches the webhook first and returns
                `would_delete` (its current record), never a bare echo of
                `webhook_id`.
        """
        c = _client()

        if op == "list":
            return {"webhooks": _run(lambda: c.list_webhooks())}

        if op == "get":
            _need(webhook_id, "webhook_id", op)
            return _run(lambda: c.get_webhook(webhook_id))

        if op == "create":
            _need(trigger_type, "trigger_type", op)
            _need(url, "url", op)
            if filter is not None and trigger_type != "form_submission":
                raise _bad(
                    "op='create' : `filter` n'est valide que pour "
                    "trigger_type='form_submission' — Webflow refuse toute "
                    f"autre combinaison (reçu trigger_type={trigger_type!r}).")
            if dry_run:
                preview = {"triggerType": trigger_type, "url": url}
                if filter is not None:
                    preview["filter"] = filter
                return {"dry_run": True, "would_create": preview}
            return _run(lambda: c.create_webhook(trigger_type, url, filter=filter))

        if op == "delete":
            _need(webhook_id, "webhook_id", op)
            if dry_run:
                current = _run(lambda: c.get_webhook(webhook_id))
                return {"dry_run": True, "webhook_id": webhook_id,
                        "would_delete": current}
            _run(lambda: c.delete_webhook(webhook_id))
            return {}

        raise _bad("op doit être 'list', 'get', 'create' ou 'delete'.")

    @mcp.tool()
    def webflow_forms(
        op: Literal["list", "get"] = "list",
        form_id: Optional[str] = None,
        offset: int = 0,
        max_results: int = 100,
    ) -> dict:
        """Forms configured on the pinned Webflow site — their schema (which
        fields, which page they live on), not the leads people submitted (see
        `webflow_submissions` for that). No write op: a form is built in
        Webflow's visual editor, not created/edited via API.

        Returns —
            list: `{"forms": [...], "pagination": {"total", "offset", "limit"}}`
                — each form has `id`, `displayName`, `pageId`/`pageName`,
                `fields` (per-field type/placeholder/visibility),
                `responseSettings` (redirect URL, email confirmation).
            get: the same shape for one form.

        Args:
            op: "list" (default) — every form on the site, paginated.
                "get" — one form's full schema by `form_id`.
            form_id: required for op="get".
            offset, max_results: op="list" pagination — max_results capped at
                100 server-side.
        """
        c = _client()
        if op == "list":
            return _run(lambda: c.list_forms(offset=offset,
                                             limit=min(max_results, 100)))
        if op == "get":
            _need(form_id, "form_id", op)
            return _run(lambda: c.get_form(form_id))
        raise _bad("op doit être 'list' ou 'get'.")

    @mcp.tool()
    def webflow_submissions(
        op: Literal["list", "get", "update", "delete"] = "list",
        form_id: Optional[str] = None,
        submission_id: Optional[str] = None,
        offset: int = 0,
        max_results: int = 100,
        form_submission_data: Optional[dict] = None,
        dry_run: bool = False,
    ) -> dict:
        """Form submissions — the actual LEAD DATA a real visitor typed into
        one of the site's forms (see `webflow_forms` for the form catalog
        itself). This is the tool for "what leads came in through the
        Webflow contact form" style questions.

        ⚠️ **No create op exists** — a submission only comes into being when
        a visitor submits the live form; there is no API to fabricate one.

        `op`:
        - **"list"** (default): every submission of ONE form (`form_id`
          required), paginated.
        - **"get"**: one submission by `submission_id` (site-scoped — no
          `form_id` needed here, only op="list" requires it).
        - **"update"**: set HIDDEN field values on a submission
          (`form_submission_data`). Does NOT edit what the visitor actually
          typed — Webflow does not allow that after the fact; only fields
          declared as hidden on the form's schema (`webflow_forms(op="get")`)
          are writable here, anything else is silently ignored by Webflow.
        - **"delete"**: permanently remove a submission (e.g. a GDPR removal
          request). Irreversible.

        Returns —
            list: `{"formSubmissions": [...], "pagination": {"total", "offset",
                "limit"}}` — each submission has `id`, `formId`,
                `dateSubmitted`, `formResponse` (the visitor's answers,
                field name → value), `localeId`.
            get: the submission.
            update: the updated submission, or `{"dry_run": true,
                "submission_id", "changes"}`.
            delete: `{}`, or `{"dry_run": true, "submission_id",
                "would_delete"}`.

        Args:
            op: list (default) | get | update | delete.
            form_id: required for op="list" only.
            submission_id: required for op="get"/"update"/"delete".
            offset, max_results: op="list" pagination — max_results capped at
                100 server-side.
            form_submission_data: op="update" — `{"<hidden field name>":
                "<value>"}`.
            dry_run: update/delete — fetches the submission first and returns
                a real diff (`changes: {field: {"from", "to"}}`, update) or
                `would_delete` (the full current record, delete) — never a
                bare echo of the input.
        """
        c = _client()

        if op == "list":
            _need(form_id, "form_id", op)
            return _run(lambda: c.list_form_submissions(
                form_id, offset=offset, limit=min(max_results, 100)))

        if op == "get":
            _need(submission_id, "submission_id", op)
            return _run(lambda: c.get_form_submission(submission_id))

        if op == "update":
            _need(submission_id, "submission_id", op)
            _need(form_submission_data, "form_submission_data", op)
            if dry_run:
                current = _run(lambda: c.get_form_submission(submission_id))
                current_hidden = current.get("formResponse", {})
                return {"dry_run": True, "submission_id": submission_id,
                        "changes": {k: {"from": current_hidden.get(k), "to": v}
                                    for k, v in form_submission_data.items()}}
            return _run(lambda: c.update_form_submission(
                submission_id, form_submission_data))

        if op == "delete":
            _need(submission_id, "submission_id", op)
            if dry_run:
                current = _run(lambda: c.get_form_submission(submission_id))
                return {"dry_run": True, "submission_id": submission_id,
                        "would_delete": current}
            _run(lambda: c.delete_form_submission(submission_id))
            return {}

        raise _bad("op doit être 'list', 'get', 'update' ou 'delete'.")

    @mcp.tool()
    def webflow_pages(
        op: Literal["list", "get", "update", "content"] = "list",
        page_id: Optional[str] = None,
        title: Optional[str] = None,
        slug: Optional[str] = None,
        seo: Optional[dict] = None,
        open_graph: Optional[dict] = None,
        offset: int = 0,
        max_results: int = 100,
        locale_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Pages of the pinned Webflow site — metadata (title, slug, SEO, Open
        Graph) and READ-ONLY static content (the actual text on the page).

        ⚠️ **There is no op to WRITE a page's body content.** Webflow's API
        only allows editing a static page's content on a SECONDARY locale of
        the site — never the primary/default one — so on a single-language
        site (the common case) there is structurally no API path to edit
        page copy, only metadata. `op="content"` is read-only for that
        reason; if you need to change what a page says, that's a Designer
        edit, not something this tool can do.

        ⚠️ **`op="update"` takes effect IMMEDIATELY on the live site** — unlike
        CMS items (`webflow_cms`), a page has no draft/publish gate: a title
        or SEO change is visible to real visitors and search engines right
        away. Always `dry_run` first if you're not certain.

        `op`:
        - **"list"** (default): every page of the site, paginated.
        - **"get"**: one page's metadata by `page_id`.
        - **"update"**: change `title`/`slug`/`seo`/`open_graph` on one page,
          LIVE IMMEDIATELY (only the given fields change — this does NOT
          touch the page's body content, only its metadata).
        - **"content"**: READ the page's static text nodes (the actual
          headings/paragraphs) — `{"nodes": [{"id", "type", "text": {"html",
          "text"}, "attributes"}], "pagination"}`.

        Returns —
            list: `{"pages": [...], "pagination": {"total", "offset", "limit"}}`.
            get: the page metadata.
            update: the updated page metadata, or `{"dry_run": true,
                "page_id", "changes"}`.
            content: `{"pageId", "nodes": [...], "pagination", "lastUpdated"}`.

        Args:
            op: list (default) | get | update | content.
            page_id: required for get/update/content.
            title, slug, seo, open_graph: op="update" only — `seo` =
                `{"title", "description"}`, `open_graph` = `{"title",
                "titleCopied", "description", "descriptionCopied"}`.
            offset, max_results: op="list"/"content" pagination — capped at
                100 server-side.
            locale_id: op="list"/"get"/"update"/"content" — target a specific
                locale's version of the page (metadata/content differ per
                locale on a multi-locale site). Omit for the primary locale.
            dry_run: op="update" only — fetches the page first and returns a
                real diff (`changes: {field: {"from", "to"}}`), makes NO
                write call. Never a bare echo of the input.
        """
        c = _client()

        if op == "list":
            return _run(lambda: c.list_pages(offset=offset,
                                             limit=min(max_results, 100),
                                             locale_id=locale_id))

        _need(page_id, "page_id", op)

        if op == "get":
            return _run(lambda: c.get_page(page_id))

        if op == "update":
            # (clé webflow_pages, clé brute Webflow, valeur demandée)
            requested = [
                ("title", "title", title), ("slug", "slug", slug),
                ("seo", "seo", seo), ("open_graph", "openGraph", open_graph),
            ]
            changed = [(k, raw_k, v) for k, raw_k, v in requested if v is not None]
            if not changed:
                raise _bad("op='update' requiert au moins un de title/slug/"
                           "seo/open_graph.")
            if dry_run:
                current = _run(lambda: c.get_page(page_id))
                return {"dry_run": True, "page_id": page_id,
                        "changes": {k: {"from": current.get(raw_k), "to": v}
                                    for k, raw_k, v in changed}}
            return _run(lambda: c.update_page(
                page_id, title=title, slug=slug, seo=seo,
                open_graph=open_graph, locale_id=locale_id))

        if op == "content":
            return _run(lambda: c.get_page_content(
                page_id, offset=offset, limit=min(max_results, 100),
                locale_id=locale_id))

        raise _bad("op doit être 'list', 'get', 'update' ou 'content'.")

    @mcp.tool()
    def webflow_site_publish(
        publish_to_webflow_subdomain: bool = False,
        custom_domains: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Publish the ENTIRE site (every page) — distinct from
        `webflow_publish`, which only pushes CMS items live. Rate-limited by
        Webflow to one publish per minute.

        Args:
            publish_to_webflow_subdomain: publish to the site's
                *.webflow.io subdomain.
            custom_domains: publish to these custom domain ids (see
                `webflow_cms(op="site")` for the site's configured domains).
                At least one of this or `publish_to_webflow_subdomain` is
                required.
            dry_run: makes NO publish call, returns what would be targeted.
        """
        if not custom_domains and not publish_to_webflow_subdomain:
            raise _bad(
                "fournir custom_domains et/ou "
                "publish_to_webflow_subdomain=True — au moins une cible de "
                "publication doit être désignée.")
        if dry_run:
            return {"dry_run": True, "would_publish": {
                "customDomains": custom_domains or [],
                "publishToWebflowSubdomain": publish_to_webflow_subdomain,
            }}
        c = _client()
        return _run(lambda: c.publish_site(
            custom_domains=custom_domains,
            publish_to_webflow_subdomain=publish_to_webflow_subdomain))
