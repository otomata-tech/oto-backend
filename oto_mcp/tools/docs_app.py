"""Docs — rendered MCP App variant (`oto_doc_app`): read/browse a project's pages
(including the org KB) INLINE in the conversation, instead of `oto_doc`'s JSON.

Same pattern as `data_app` (SEP-1865, prefab_ui): guarded optional import
(extra `fastmcp[apps]` absent → the tool does not register, `oto_doc` remains the
default/agent path). READ-ONLY — every write goes through `oto_doc`.
Spine (loaded explicitly by register_all, outside the activation gate).

**Two channels, two readers** (MCP Apps extension): the host paints the card with
`structuredContent` and gives the MODEL only the text `content`. Returning the bare card
made FastMCP put the "[Rendered Prefab UI]" marker in the text: the model read
nothing of the page and improvised (signal #1083, 18/09/2026). Each view therefore renders
its content AS TEXT too — the whole page, the tree, the excerpts (`_rendu`).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from .. import access, db, org_store, ownership

PROJECT_RTYPE = "project"


def register(mcp: FastMCP) -> None:
    try:
        from prefab_ui.components import (  # type: ignore
            Card, Column, DataTable, DataTableColumn, Heading, Markdown, Text,
        )
    # noqa: SILENT — extra `apps` absent ⇒ no app, the JSON tools are enough
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    def _rendu(card, texte: str) -> ToolResult:
        """The card to the host (`structuredContent`), the same content to the model (text).
        Never one without the other: see the module header."""
        return ToolResult(content=[TextContent(type="text", text=texte)],
                          structured_content=card)

    def _message_card(title: str, message: str) -> ToolResult:
        with Card() as card:
            with Column(gap=4):
                Heading(title)
                Text(message)
        return _rendu(card, f"{title} — {message}")

    def _can_read(sub: str, project_id: int) -> bool:
        return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), "read")

    def _kb_project_id(sub: str) -> Optional[int]:
        """KB of the active org — READ-only resolution (no lazy creation
        here: creation goes through the dashboard's REST route, the MCP verb
        having been removed; a read app mutates nothing).

        Through the ANCHOR `orgs.kb_project_id`, like `capabilities/kb.py`. This function
        used to look for the project whose NAME equals `KB_NAME`, although identification by
        name died in batch 3 (workstream 0.3): an org that renamed its base lost
        `oto_doc_app` with no argument, which answered "No project targeted" while
        the KB was there, anchored and readable (#527)."""
        org = access.current_org(sub)
        if org is None:
            return None
        return org_store.get_kb_project_id(org)

    def _tree_rows(docs: list) -> list:
        """Flatten the tree (parent_id) into indented rows, DFS order — children
        under their parent. A dangling parent_id (orphan page) is lifted to the root
        rather than disappearing."""
        ids = {d["id"] for d in docs}
        children: dict = {}
        for d in docs:
            pid = d.get("parent_id")
            key = pid if pid in ids else None
            children.setdefault(key, []).append(d)
        out = []

        def walk(parent, depth):
            for d in children.get(parent, []):
                prefix = (" " * depth + "└ ") if depth else ""
                out.append({
                    "page": prefix + (d.get("title") or f"#{d['id']}"),
                    "type": d.get("kind") or "doc",
                    "maj": str(d.get("updated_at") or "")[:16],
                    "id": d["id"],
                })
                walk(d["id"], depth + 1)

        walk(None, 0)
        return out

    def _strip_hl(s: str) -> str:
        """ts_headline tags matches as <b>…</b> — noise in a cell."""
        return (s or "").replace("<b>", "").replace("</b>", "")

    @mcp.tool(app=True)
    def oto_doc_app(
        project_id: int | None = None,
        doc_id: int | None = None,
        query: str | None = None,
    ):  # no `-> Card` return annotation: same gotcha as data_app (hints
        # are resolved against the module globals when the schema is built, but `Card` is
        # imported LOCAL to register() → fatal NameError at startup, cf. datastore #69).
        """Rendered docs browser (MCP App / interactive card) — READ ONLY.

        Visual variant of `oto_doc` that renders pages INLINE instead of returning
        JSON. WITHOUT arguments = the tree of the active org's HISTORICAL docs
        project (whatever it is named — resolved by its anchor);
        pass `project_id` to browse the docs of the project you actually mean.
        With `project_id` = that project's pages tree
        (children indented under parents). With `doc_id` = ONE page, markdown
        rendered. With `query` (+ optional `project_id`) = full-text hits with
        snippets, accent-insensitive.

        Use when the user wants to *see* a page or explore a project's docs
        without leaving the chat. The text result gives YOU the same content (the
        whole page, the tree, the hits) — read it, never summarize from the card.
        For raw JSON or ANY write (create/update/move/share), use `oto_doc`.

        Args:
            project_id: project whose pages to browse ; omit = the active org's
                historical docs project (resolved by its anchor).
            doc_id: render ONE page (title + markdown body). Takes precedence.
            query: full-text search in the project's pages (title + body).
        """
        sub = access.current_user_sub_or_raise()

        if doc_id is not None:
            row = db.get_doc_by_id(int(doc_id))
            if row is None or not _can_read(sub, row["project_id"]):
                return _message_card("Page not found",
                                     f"No accessible page #{doc_id}.")
            meta = f"{row.get('kind') or 'doc'} · updated {str(row.get('updated_at') or '')[:16]}"
            title = str(row.get("title") or f"#{doc_id}")
            body = row.get("body_md") or "*(empty page)*"
            # Le lien public est rangé haché : on dit qu'il existe, pas son adresse.
            lien = "shared by public link" if row.get("public") else None
            with Card() as card:
                with Column(gap=4):
                    Heading(title)
                    Text(meta)
                    Markdown(body)
                    if lien:
                        Text(lien)
            entete = [f"# {title}", f"page #{doc_id} · {meta}"] + ([lien] if lien else [])
            return _rendu(card, "\n".join(entete) + "\n\n" + body)

        pid = int(project_id) if project_id is not None else _kb_project_id(sub)
        if pid is None:
            return _message_card(
                "No project targeted",
                "No documents project anchored in your active org — pass "
                "`project_id` (cf. oto_project op=list) to see the documents of the "
                "project you mean.",
            )
        project = db.get_project_by_id(pid)
        if project is None or not _can_read(sub, pid):
            return _message_card("Project not found",
                                 f"No accessible project #{pid}.")

        if query and query.strip():
            hits = db.search_docs_in_project(pid, query.strip())
            rows = [{"page": h.get("title"), "type": h.get("kind") or "doc",
                     "extrait": _strip_hl(h.get("snippet") or ""), "id": h["id"]}
                    for h in hits]
            titre = f"« {query.strip()} » — {project.get('name')}"
            compte = f"{len(rows)} page(s) found · read: oto_doc_app doc_id=<id>"
            with Card() as card:
                with Column(gap=4):
                    Heading(titre)
                    Text(compte)
                    if rows:
                        cols = [DataTableColumn(key="page", header="Page", sortable=True),
                                DataTableColumn(key="type", header="Type", sortable=True),
                                DataTableColumn(key="extrait", header="Excerpt"),
                                DataTableColumn(key="id", header="Id")]
                        DataTable(columns=cols, rows=rows, search=False,
                                  paginated=len(rows) > 20, pageSize=20)
                    else:
                        Text("No page matches.")
            lignes = [f"- {r['page']} · {r['type']} · #{r['id']} — {r['extrait']}"
                      for r in rows] or ["No page matches."]
            return _rendu(card, "\n".join([titre, compte, ""] + lignes))

        docs = db.list_docs_for_project(pid)
        rows = _tree_rows(docs)
        titre = str(project.get("name") or f"Project #{pid}")
        compte = f"{len(rows)} page(s) · read: oto_doc_app doc_id=<id>"
        with Card() as card:
            with Column(gap=4):
                Heading(titre)
                Text(compte)
                if rows:
                    cols = [DataTableColumn(key="page", header="Page"),
                            DataTableColumn(key="type", header="Type", sortable=True),
                            DataTableColumn(key="maj", header="Updated", sortable=True),
                            DataTableColumn(key="id", header="Id")]
                    DataTable(columns=cols, rows=rows, search=True,
                              paginated=len(rows) > 20, pageSize=20)
                else:
                    Text("No page — create one with oto_doc (op=create).")
        lignes = [f"{r['page']} · {r['type']} · updated {r['maj']} · #{r['id']}" for r in rows] \
            or ["No page — create one with oto_doc (op=create)."]
        return _rendu(card, "\n".join([f"{titre} (project #{pid})", compte, ""] + lignes))
