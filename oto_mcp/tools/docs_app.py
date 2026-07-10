"""Docs — variante MCP App rendue (`oto_doc_app`) : lire/parcourir les pages d'un
projet (dont la KB d'org) INLINE dans la conversation, au lieu du JSON d'`oto_doc`.

Même patron que `data_app` (SEP-1865, prefab_ui) : import optionnel gardé
(extra `fastmcp[apps]` absent → le tool ne s'enregistre pas, `oto_doc` reste la
voie par défaut/agent). Lecture SEULE — toute écriture passe par `oto_doc`.
Spine (chargé explicitement par register_all, hors gate d'activation).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access, db, ownership
from ..capabilities.kb import KB_NAME

PROJECT_RTYPE = "project"


def register(mcp: FastMCP) -> None:
    try:
        from prefab_ui.components import (  # type: ignore
            Card, Column, DataTable, DataTableColumn, Heading, Markdown, Text,
        )
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    def _message_card(title: str, message: str) -> "Card":
        with Card() as card:
            with Column(gap=4):
                Heading(title)
                Text(message)
        return card

    def _can_read(sub: str, project_id: int) -> bool:
        return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), "read")

    def _kb_project_id(sub: str) -> Optional[int]:
        """KB de l'org active — résolution LECTURE seule (pas de création paresseuse
        ici : c'est `oto_kb` qui crée ; une app de lecture ne mute rien)."""
        org = access.current_org(sub)
        if org is None:
            return None
        for p in db.list_projects_for_owners([("org", str(org))]):
            if p.get("name") == KB_NAME:
                return p["id"]
        return None

    def _tree_rows(docs: list) -> list:
        """Aplatis l'arbre (parent_id) en lignes indentées, ordre DFS — les enfants
        sous leur parent. Un parent_id pendouillant (page orpheline) remonte en racine
        plutôt que de disparaître."""
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
        """ts_headline balise les matchs en <b>…</b> — du bruit dans une cellule."""
        return (s or "").replace("<b>", "").replace("</b>", "")

    @mcp.tool(app=True)
    def oto_doc_app(
        project_id: int | None = None,
        doc_id: int | None = None,
        query: str | None = None,
    ):  # pas d'annotation de retour `-> Card` : même gotcha que data_app (les hints
        # sont résolus contre les globals du module au build du schéma, or `Card` est
        # importé LOCAL à register() → NameError fatal au démarrage, cf. datastore #69).
        """Rendered docs browser (MCP App / interactive card) — READ ONLY.

        Visual variant of `oto_doc` that renders pages INLINE instead of returning
        JSON. WITHOUT arguments = the tree of the active org's KNOWLEDGE BASE
        (« Base de connaissance »). With `project_id` = that project's pages tree
        (children indented under parents). With `doc_id` = ONE page, markdown
        rendered. With `query` (+ optional `project_id`) = full-text hits with
        snippets, accent-insensitive.

        Use when the user wants to *see* a page or explore the docs/KB without
        leaving the chat. For raw JSON or ANY write (create/update/move/share),
        use `oto_doc`.

        Args:
            project_id: project whose pages to browse ; omit = the active org's
                knowledge base (see `oto_kb`).
            doc_id: render ONE page (title + markdown body). Takes precedence.
            query: full-text search in the project's pages (title + body).
        """
        sub = access.current_user_sub_or_raise()

        if doc_id is not None:
            row = db.get_doc_by_id(int(doc_id))
            if row is None or not _can_read(sub, row["project_id"]):
                return _message_card("Page introuvable",
                                     f"Aucune page #{doc_id} accessible.")
            meta = f"{row.get('kind') or 'doc'} · maj {str(row.get('updated_at') or '')[:16]}"
            tok = row.get("public_token")
            with Card() as card:
                with Column(gap=4):
                    Heading(str(row.get("title") or f"#{doc_id}"))
                    Text(meta)
                    Markdown(row.get("body_md") or "*(page vide)*")
                    if tok:
                        from ..capabilities.docs import _public_doc_url
                        Text(f"lien public : {_public_doc_url(tok)}")
            return card

        pid = int(project_id) if project_id is not None else _kb_project_id(sub)
        if pid is None:
            return _message_card(
                "Aucun projet ciblé",
                "Pas de base de connaissance dans ton org active — passe `project_id` "
                "(cf. oto_project op=list) ou crée la KB via oto_kb.",
            )
        project = db.get_project_by_id(pid)
        if project is None or not _can_read(sub, pid):
            return _message_card("Projet introuvable",
                                 f"Aucun projet #{pid} accessible.")

        if query and query.strip():
            hits = db.search_docs_in_project(pid, query.strip())
            rows = [{"page": h.get("title"), "type": h.get("kind") or "doc",
                     "extrait": _strip_hl(h.get("snippet") or ""), "id": h["id"]}
                    for h in hits]
            with Card() as card:
                with Column(gap=4):
                    Heading(f"« {query.strip()} » — {project.get('name')}")
                    Text(f"{len(rows)} page(s) trouvée(s) · lire : oto_doc_app doc_id=<id>")
                    if rows:
                        cols = [DataTableColumn(key="page", header="Page", sortable=True),
                                DataTableColumn(key="type", header="Type", sortable=True),
                                DataTableColumn(key="extrait", header="Extrait"),
                                DataTableColumn(key="id", header="Id")]
                        DataTable(columns=cols, rows=rows, search=False,
                                  paginated=len(rows) > 20, pageSize=20)
                    else:
                        Text("Aucune page ne correspond.")
            return card

        docs = db.list_docs_for_project(pid)
        rows = _tree_rows(docs)
        with Card() as card:
            with Column(gap=4):
                Heading(str(project.get("name") or f"Projet #{pid}"))
                Text(f"{len(rows)} page(s) · lire : oto_doc_app doc_id=<id>")
                if rows:
                    cols = [DataTableColumn(key="page", header="Page"),
                            DataTableColumn(key="type", header="Type", sortable=True),
                            DataTableColumn(key="maj", header="Maj", sortable=True),
                            DataTableColumn(key="id", header="Id")]
                    DataTable(columns=cols, rows=rows, search=True,
                              paginated=len(rows) > 20, pageSize=20)
                else:
                    Text("Aucune page — crée-en avec oto_doc (op=create).")
        return card
