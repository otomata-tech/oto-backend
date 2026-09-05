"""Notion — pages, databases, blocks (read + write).

Wrappe `oto.tools.notion.lib.notion_client.NotionClient`. Token d'intégration
résolu par appel via `access.resolve_api_key("notion")` — byo. **Cache disque
désactivé** (`cache_enabled=False`) : le cache fichier n'est pas clefé par token
→ fuite cross-user sur un host multi-utilisateur.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /v1/users/me` (« Retrieve your token's bot user »). Ce que la doc
    Notion établit :

    - **authentifié** — Bearer token (jeton d'intégration), comme le reste de
      l'API ;
    - **sans effet de bord** — une lecture du bot user associé au jeton ;
    - **le coût** — aucune mention de coût ni de limite de débit particulière
      pour cet appel. Absence de mention, indice, pas une preuve.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope
    granulaire ici — Notion n'accorde pas de permissions PAR CAPACITÉ sur un
    jeton d'intégration, seulement un PARTAGE par page/base (côté workspace,
    invisible depuis l'API). `cache_enabled=False`, comme `_client()` : le
    cache disque n'est pas clefé par jeton (cf. docstring du module).
    """
    from oto.tools.notion.lib.notion_client import NotionClient

    infos = NotionClient(token=fields["key"], cache_enabled=False)._request(
        "GET", "users/me", use_cache=False) or {}
    if not infos.get("id"):
        raise RuntimeError(
            "Notion a répondu sans identifier de bot user pour ce jeton — "
            f"réponse inattendue : {str(infos)[:200]}")


def register(mcp: FastMCP) -> None:
    from oto.tools.notion.lib.notion_client import NotionClient

    connector_verify.register("notion", _verify)

    def _client() -> NotionClient:
        key, _ = access.resolve_api_key("notion")
        return NotionClient(token=key, cache_enabled=False)

    @mcp.tool()
    def notion_search(
        query: str,
        filter_type: Optional[str] = None,
        sort: str = "relevance",
    ) -> dict:
        """Search the workspace (pages + databases shared with the integration).

        Args:
            filter_type: "page" or "database" to restrict object type.
            sort: "relevance" (default) or "last_edited_time".
        """
        return _client().search(query, filter_type=filter_type, sort=sort)

    @mcp.tool()
    def notion_get_page(page_id: str) -> dict:
        """Get a page's metadata/properties (not its block content)."""
        return _client().get_page(page_id)

    @mcp.tool()
    def notion_get_blocks(page_id: str, recursive: bool = False) -> dict:
        """Get a page's block content. `recursive` fetches nested children too."""
        return _client().get_page_blocks(page_id, recursive=recursive)

    @mcp.tool()
    def notion_get_database(database_id: str) -> dict:
        """Get a database's schema (properties + data sources)."""
        return _client().get_database(database_id)

    @mcp.tool()
    def notion_query_database(
        database_id: str,
        filter_obj: Optional[dict] = None,
        sorts: Optional[list] = None,
        page_size: int = 100,
    ) -> dict:
        """Query a database's rows.

        Args:
            filter_obj: Notion filter object (e.g. {"property": "Status",
                "select": {"equals": "Done"}}).
            sorts: Notion sorts array.
        """
        return _client().query_database(
            database_id, filter_obj=filter_obj, sorts=sorts, page_size=page_size)

    @mcp.tool()
    def notion_create_page(
        parent_id: str,
        parent_type: str,
        title: str,
        properties: Optional[dict] = None,
        content: Optional[list] = None,
    ) -> dict:
        """Create a page under a parent.

        Args:
            parent_type: "page" or "database".
            properties: extra Notion property values (db rows: keyed by column).
            content: optional array of Notion block objects for the body.
        """
        return _client().create_page(
            parent_id, parent_type, title, properties=properties, content=content)

    @mcp.tool()
    def notion_update_page(
        page_id: str,
        properties: Optional[dict] = None,
        archived: Optional[bool] = None,
    ) -> dict:
        """Update a page's properties, or archive/unarchive it (`archived`)."""
        return _client().update_page(page_id, properties=properties, archived=archived)

    @mcp.tool()
    def notion_append_blocks(page_id: str, blocks: list) -> dict:
        """Append block objects to a page/block's children."""
        return _client().append_blocks(page_id, blocks)
