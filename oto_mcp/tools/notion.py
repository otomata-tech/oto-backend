"""Notion — pages, databases, blocks (read + write).

Wrappe `oto.tools.notion.lib.notion_client.NotionClient`. Token d'intégration
résolu par appel via `access.resolve_api_key("notion")` — byo. **Cache disque
désactivé** (`cache_enabled=False`) : le cache fichier n'est pas clefé par token
→ fuite cross-user sur un host multi-utilisateur.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError


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


def _zero_warning(query: str, filter_type: Optional[str],
                  edited_on: Optional[str] = None) -> str:
    """L'avertissement qu'un `notion_search` VIDE porte (otomata-tech/oto#184).

    Un jeton valide auquel rien n'est partagé répond EXACTEMENT comme un espace qui
    ne contient pas ce qu'on cherche (`results: []`), et la sonde `_verify` reste
    verte (elle ne voit que l'authentification). Le savoir existait deux fois —
    doc d'installation, commentaire de sonde — jamais là où l'agent lit : on le
    porte donc dans la réponse, au moment du zéro.

    ⚠️ Formulé comme une POSSIBILITÉ À VÉRIFIER, pas comme un diagnostic : la
    lecture « requête vide + zéro objet ⟹ rien de partagé » n'a pas été éprouvée
    contre l'API Notion, et un diagnostic faux enverrait réparer un partage sain."""
    geste = ("partager la page ou la base voulue avec l'intégration, côté "
             "workspace Notion (menu `...` → Connexions)")
    if edited_on:
        return (
            f"Zéro objet édité le {edited_on} (jour UTC). Sur Notion, ce zéro ne "
            "distingue PAS « rien n'a bougé ce jour-là » de « rien n'est partagé avec "
            "l'intégration ». Pour trancher : relance `notion_search` avec `query=\"\"`, "
            "sans `filter_type` ni `edited_on` — si elle rend aussi zéro, l'intégration "
            f"ne voit vraisemblablement rien : {geste}.")
    if query or filter_type:
        return (
            "Zéro résultat. Sur Notion, un zéro ne distingue PAS « rien ne "
            "correspond » de « rien n'est partagé avec l'intégration » (le jeton "
            "s'authentifie dans les deux cas, la sonde reste verte). Pour trancher : "
            "relance `notion_search` avec `query=\"\"` et sans `filter_type` — si "
            f"elle rend aussi zéro, l'intégration ne voit vraisemblablement rien : "
            f"{geste}.")
    return (
        "Zéro objet sur une recherche SANS filtre : l'intégration ne voit "
        "vraisemblablement rien — aucune page ni base ne lui est partagée (le jeton "
        "s'authentifie, la sonde reste verte, et Notion ne le dit pas). À vérifier "
        f"avant d'agir, puis {geste}. Ce n'est PAS un credential à reposer.")


def _invalid_params(call):
    """Run `call`; a ValueError from the client (bad argument) becomes INVALID_PARAMS."""
    try:
        return call()
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e


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
        edited_on: Optional[str] = None,
        cursor: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Search the workspace (pages + databases shared with the integration).

        The integration sees ONLY what was shared with it in Notion: an empty
        `results` may mean "nothing shared", not "nothing matches". An empty
        answer carries a `warning` saying how to tell the two apart.

        Returns ONE page (at most 100 objects). `has_more: true` means there is
        more: pass the answer's `next_cursor` back as `cursor`, same other args.

        `edited_on` answers "what changed that day": EVERY object whose
        `last_edited_time` falls on that UTC calendar day, most recent first,
        in one answer (walked server-side until the day is passed — cost tracks
        what changed, not workspace size). `sort` and `cursor` do not apply then.

        `fields` keeps only these keys in each object; the envelope
        (`has_more`, `next_cursor`) always stays.

        Args:
            query: text to match; "" lists everything the integration can see.
            filter_type: "page" or "database" to restrict object type
                (databases come back as `data_source` objects).
            sort: "relevance" (default) or "last_edited_time".
            edited_on: "YYYY-MM-DD" (UTC day) — only objects last edited that day.
            cursor: `next_cursor` of the previous answer, to read the next page.
            fields: keys kept in each object (e.g. ["id", "url", "last_edited_time"]).
        """
        client = _client()
        if edited_on and cursor:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                "`edited_on` rend déjà tout le jour en une réponse : il ne se "
                "pagine pas, retire `cursor`.")))
        if edited_on:
            try:
                objets = client.search_edited_on(
                    edited_on, filter_type=filter_type, query=query)
            except ValueError as e:  # date mal formée — le seul ValueError de la méthode
                raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e
            result = {"results": objets, "edited_on": edited_on}
        else:
            result = client.search(query, filter_type=filter_type, sort=sort,
                                   start_cursor=cursor)
        if not result.get("results"):
            result = {**result,
                      "warning": _zero_warning(query, filter_type, edited_on)}
        return output_projection.project(result, items_path="results", fields=fields)

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
        """Get a database's schema: its columns (`properties`) and data sources.

        `database_id` may be a database id or a data source id (what search returns).
        """
        return _client().get_database(database_id)

    @mcp.tool()
    def notion_query_database(
        database_id: str,
        filter_obj: Optional[dict] = None,
        sorts: Optional[list] = None,
        page_size: int = 100,
        cursor: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Query a database's rows — one page (`has_more` + `next_cursor`).

        `database_id` may be a database id or a data source id.

        Args:
            filter_obj: Notion filter object (e.g. {"property": "Status",
                "select": {"equals": "Done"}}).
            sorts: Notion sorts array.
            cursor: `next_cursor` of the previous answer, to read the next page.
            fields: keys kept in each row (e.g. ["id", "url", "properties"]).
        """
        result = _client().query_database(
            database_id, filter_obj=filter_obj, sorts=sorts, page_size=page_size,
            start_cursor=cursor)
        return output_projection.project(result, items_path="results", fields=fields)

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
            parent_type: "page" or "database". For "database", `parent_id`
                may be a database id or a data source id — never a linked
                view (use the source database).
            title: goes in the database's title column, whatever its name.
            properties: extra Notion property values (db rows: keyed by column).
            content: optional array of Notion block objects for the body
                (any length).
        """
        return _invalid_params(lambda: _client().create_page(
            parent_id, parent_type, title, properties=properties, content=content))

    @mcp.tool()
    def notion_update_page(
        page_id: str,
        properties: Optional[dict] = None,
        in_trash: Optional[bool] = None,
        icon: Optional[dict] = None,
        cover: Optional[dict] = None,
        archived: Optional[bool] = None,
    ) -> dict:
        """Update a page's properties, icon or cover, or trash/restore it.

        To move a page elsewhere, use notion_move_page.

        Args:
            in_trash: true moves the page to the trash, false restores it.
            icon: e.g. {"type": "emoji", "emoji": "📝"}.
            archived: old name of `in_trash`.
        """
        return _client().update_page(page_id, properties=properties, archived=archived,
                                     in_trash=in_trash, icon=icon, cover=cover)

    @mcp.tool()
    def notion_move_page(page_id: str, parent_id: str, parent_type: str = "page") -> dict:
        """Move a page under another page, or into a database.

        Args:
            parent_type: "page", or "database" (a database or data source id).
        """
        return _invalid_params(lambda: _client().move_page(page_id, parent_id, parent_type))

    @mcp.tool()
    def notion_append_blocks(
        page_id: str, blocks: list, position: Optional[str] = None,
    ) -> dict:
        """Add block objects to a page/block — any number, sent in batches.

        Args:
            position: "end" (default), "start" (top of the page), or the id of
                an existing child block to insert right after it.
        """
        return _client().append_blocks(page_id, blocks, position=position)

    @mcp.tool()
    def notion_update_block(block_id: str, block: dict) -> dict:
        """Edit one block in place, e.g. {"paragraph": {"rich_text": [...]}} or
        {"to_do": {"checked": true}}. Its children cannot be edited this way."""
        return _client().update_block(block_id, block)

    @mcp.tool()
    def notion_delete_block(block_id: str) -> dict:
        """Move one block to the trash (restorable from Notion)."""
        return _client().delete_block(block_id)

    @mcp.tool()
    def notion_get_markdown(page_id: str) -> dict:
        """Get a page's whole content as Markdown — lighter than notion_get_blocks.

        `truncated: true`: some parts were not loaded; their ids are in
        `unknown_block_ids` and each can be read the same way.
        """
        return _client().get_markdown(page_id)

    @mcp.tool()
    def notion_edit_markdown(
        page_id: str,
        replacements: Optional[list[dict]] = None,
        new_content: Optional[str] = None,
        insert: Optional[str] = None,
        position: str = "end",
        allow_deleting_content: bool = False,
    ) -> dict:
        """Edit a page's content as Markdown — give exactly ONE of the three modes.

        Args:
            replacements: targeted edits, [{"old_str": ..., "new_str": ...,
                "replace_all_matches": false}] (max 100). Each `old_str` must
                match exactly once unless `replace_all_matches`.
            new_content: replaces the WHOLE page content.
            insert: Markdown added at `position` ("start" or "end").
            allow_deleting_content: required if the edit removes child pages
                or databases (refused otherwise).
        """
        return _invalid_params(lambda: _client().edit_markdown(
            page_id, replacements=replacements, new_content=new_content,
            insert=insert, position=position,
            allow_deleting_content=allow_deleting_content))

    @mcp.tool()
    def notion_get_comments(
        page_id: str, cursor: Optional[str] = None, fields: Optional[list[str]] = None,
    ) -> dict:
        """List the open comments on a page or block (`page_id` takes a block id too).

        Each comment carries its `discussion_id` — pass it to notion_add_comment
        to reply in the thread. The integration needs the "read comments"
        capability (off by default in Notion).
        """
        result = _client().list_comments(page_id, start_cursor=cursor)
        return output_projection.project(result, items_path="results", fields=fields)

    @mcp.tool()
    def notion_add_comment(
        text: str,
        page_id: Optional[str] = None,
        block_id: Optional[str] = None,
        discussion_id: Optional[str] = None,
    ) -> dict:
        """Comment on a page or a block, or reply in an existing discussion.

        Give exactly one target. `text` is Markdown (inline formatting only).
        The integration needs the "insert comments" capability (off by default).
        """
        return _invalid_params(lambda: _client().add_comment(
            text, page_id=page_id, block_id=block_id, discussion_id=discussion_id))

    @mcp.tool()
    def notion_edit_comment(
        comment_id: str, text: Optional[str] = None, delete: bool = False,
    ) -> dict:
        """Rewrite (`text`) or delete (`delete=true`) a comment the integration wrote."""
        client = _client()
        if delete:
            return client.delete_comment(comment_id)
        if not text:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                "Give `text` to rewrite the comment, or `delete=true`.")))
        return client.update_comment(comment_id, text)

    @mcp.tool()
    def notion_create_database(
        parent_page_id: str,
        title: Optional[str] = None,
        properties: Optional[dict] = None,
        is_inline: bool = False,
        database_type: Optional[str] = None,
    ) -> dict:
        """Create a database inside a page.

        Args:
            properties: column schema, e.g. {"Name": {"title": {}},
                "Status": {"select": {"options": [{"name": "Todo"}]}},
                "Due": {"date": {}}}. A "Name" title column is added if none.
            is_inline: show it inside the page rather than as a sub-page.
            database_type: "tasks", "projects" or "skills" for Notion's own
                schema (instead of `properties`).
        """
        return _invalid_params(lambda: _client().create_database(
            parent_page_id, title=title, properties=properties, is_inline=is_inline,
            database_type=database_type))

    @mcp.tool()
    def notion_update_database(
        database_id: str,
        properties: Optional[dict] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        in_trash: Optional[bool] = None,
    ) -> dict:
        """Change a database's columns, title or description, or trash it.

        `database_id` may be a database id or a data source id. Read the
        current columns first with notion_get_database.

        Args:
            properties: column changes — add {"Due": {"date": {}}}, rename
                {"Due": {"name": "Deadline"}}, remove {"Due": null}, change
                type {"Estimate": {"number": {}}}. Select/status options: give
                the FULL list to keep ({"id": ...} or {"name": ..., "color": ...}).
        """
        return _invalid_params(lambda: _client().update_database(
            database_id, properties=properties, title=title,
            description=description, in_trash=in_trash))

    @mcp.tool()
    def notion_view(
        op: str,
        database_id: Optional[str] = None,
        view_id: Optional[str] = None,
        name: Optional[str] = None,
        view_type: str = "table",
        filter_obj: Optional[dict] = None,
        sorts: Optional[list] = None,
        configuration: Optional[dict] = None,
        clear: Optional[list[str]] = None,
        cursor: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Database views (tabs): list, read, create, change, delete.

        op=list (`database_id`) / get (`view_id`) / create (`database_id`,
        `name`, `view_type`, optional `filter_obj`, `sorts`, `configuration`) /
        update (`view_id` + any of `name`, `filter_obj`, `sorts`,
        `configuration`; `clear=["filter"|"sorts"]` resets them) /
        delete (`view_id`; a database's last view cannot be deleted).

        Args:
            view_type: table, board, list, calendar, timeline, gallery, form,
                chart, map or dashboard.
            filter_obj / sorts: same format as notion_query_database.
            configuration: type-specific settings (shown columns, group by…)
                as in the `configuration` of a view read with op=get.
        """
        client = _client()

        def need(value, label):
            if not value:
                raise McpError(ErrorData(code=INVALID_PARAMS,
                                         message=f"op={op} needs `{label}`."))
            return value

        if op == "list":
            result = client.list_views(need(database_id, "database_id"), start_cursor=cursor)
            return output_projection.project(result, items_path="results", fields=fields)
        if op == "get":
            return client.get_view(need(view_id, "view_id"))
        if op == "create":
            return _invalid_params(lambda: client.create_view(
                need(database_id, "database_id"), need(name, "name"), view_type,
                filter_obj=filter_obj, sorts=sorts, configuration=configuration))
        if op == "update":
            return _invalid_params(lambda: client.update_view(
                need(view_id, "view_id"), name=name, filter_obj=filter_obj,
                sorts=sorts, configuration=configuration, clear=clear))
        if op == "delete":
            return client.delete_view(need(view_id, "view_id"))
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            f"op {op!r}: expected list, get, create, update or delete.")))

    @mcp.tool()
    def notion_list_users(
        cursor: Optional[str] = None, fields: Optional[list[str]] = None,
    ) -> dict:
        """Workspace members, guests and bots — ids for people properties and mentions."""
        result = _client().list_users(start_cursor=cursor)
        return output_projection.project(result, items_path="results", fields=fields)
