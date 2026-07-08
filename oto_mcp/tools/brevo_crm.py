"""Brevo — CRM natif (deals, companies, tasks, notes, pipelines), API v3.

Second module du connecteur `brevo` (cf. `Connector.modules` au registre) : même
clé, même client, sous-domaine distinct. Les tools restent préfixés `brevo_` — le
namespace du gate d'activation est le 1er token (`brevo`).

Surface générique (`entity` en paramètre) plutôt que 4×4 tools : les quatre objets
partagent list/get/create/update. Les asymétries de l'API (chemin `/companies` hors
`/crm`, pagination par page, préfixe `filters[]`/`filter[]`) sont absorbées par
`oto.tools.brevo.CrmMixin`, pas ici.

Suppressions non exposées (cohérent avec `tools/brevo.py`).
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.brevo import BrevoClient

    def _client() -> BrevoClient:
        key, _ = access.resolve_api_key("brevo")
        return BrevoClient(api_key=key)


    @mcp.tool()
    def brevo_crm_list(
        entity: str,
        limit: int = 50,
        offset: int = 0,
        filters: Optional[dict] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """Liste des objets du CRM Brevo.

        Args:
            entity: `deals` | `companies` | `tasks` | `notes`.
            filters: clés BRUTES de l'entité —
                deals : `{"attributes.deal_name": "Acme", "linkedContactsIds": "12"}` ;
                companies : `{"attributes.name": "Acme"}` ;
                tasks : `{"status": "done", "type": …, "contacts": "12"}` ;
                notes : `{"entity": "deals", "entityIds": "<id>"}`.
        """
        return _client().crm_list(
            entity, limit=limit, offset=offset, filters=filters, sort_by=sort_by)

    @mcp.tool()
    def brevo_crm_get(entity: str, object_id: str) -> dict:
        """Récupère un objet du CRM Brevo par id (`deals`|`companies`|`tasks`|`notes`)."""
        return _client().crm_get(entity, object_id)

    @mcp.tool()
    def brevo_crm_create(entity: str, payload: dict) -> dict:
        """Crée un objet du CRM Brevo. Renvoie `{"id": …}`.

        `payload` en camelCase Brevo. Champs requis :
        - **deals** : `name` (+ `attributes` : `deal_stage`, `amount`, `close_date`…)
        - **companies** : `name` (+ `attributes`, `linkedContactsIds`)
        - **tasks** : `name`, `taskTypeId` (cf. `brevo_crm_meta`), `date` (ISO 8601)
        - **notes** : `text` (+ `contactIds`, `dealIds`, `companyIds`)

        Les `attributes` personnalisés se lisent via `brevo_crm_meta`.
        """
        return _client().crm_create(entity, payload)

    @mcp.tool()
    def brevo_crm_update(entity: str, object_id: str, payload: dict) -> dict:
        """Met à jour un objet du CRM Brevo (champs fournis seulement).

        Pour rattacher/détacher des objets liés, utiliser `brevo_crm_link`.
        """
        return _client().crm_update(entity, object_id, payload)

    @mcp.tool()
    def brevo_crm_link(
        entity: str,
        object_id: str,
        link_contact_ids: Optional[list[int]] = None,
        unlink_contact_ids: Optional[list[int]] = None,
        link_ids: Optional[list[str]] = None,
        unlink_ids: Optional[list[str]] = None,
    ) -> dict:
        """Rattache/détache des objets liés — `deals` et `companies` seulement.

        `link_ids`/`unlink_ids` visent l'objet complémentaire : les **companies**
        d'un deal, les **deals** d'une company.
        """
        return _client().crm_link(
            entity, object_id, link_contact_ids=link_contact_ids,
            unlink_contact_ids=unlink_contact_ids, link_ids=link_ids,
            unlink_ids=unlink_ids)

    @mcp.tool()
    def brevo_crm_meta(entity: Optional[str] = None) -> dict:
        """Métadonnées du CRM Brevo : pipelines + étapes, types de tâche, attributs.

        Args:
            entity: `deals` | `companies` → joint leurs attributs personnalisés.

        À lire avant `brevo_crm_create` : un `deal_stage` se désigne par l'`id`
        d'étape du pipeline, un `taskTypeId` par l'id de son type.
        """
        client = _client()
        out: dict[str, Any] = {
            "pipelines": client.pipelines(),
            "task_types": client.task_types(),
        }
        if entity:
            out["attributes"] = client.crm_attributes(entity)
        return out
