"""Doc — page markdown arborescente d'un projet (incrément 3, modèle produit 2026-06-27).

Un Doc appartient à un projet et **hérite de son accès** (`ownership.can_access` sur le
projet — pas d'ownership propre). Le `brief_md` du projet reste la page d'entrée ; les
Docs sont les pages, en arbre via `parent_id`. kind ∈ {doc (humain), note (agent),
source (import)}. CRUD + move, co-déclaré MCP+REST.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, ownership
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

PROJECT_RTYPE = "project"


class DocInput(BaseModel):
    op: Literal["create", "list", "get", "update", "delete", "move"]
    project_id: Optional[int] = None   # create / list
    doc_id: Optional[int] = None       # get / update / delete / move
    parent_id: Optional[int] = None    # create / move (None = 1er niveau sous le projet)
    title: Optional[str] = None
    body_md: Optional[str] = None
    kind: Optional[Literal["doc", "note", "source"]] = None


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _can(sub: str, project_id: int, want: str) -> bool:
    return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), want)


def _view(row: dict) -> dict:
    return {k: row.get(k) for k in
            ("id", "project_id", "parent_id", "title", "body_md", "kind",
             "created_at", "updated_at")}


def _doc(ctx: ResolvedCtx, inp: DocInput) -> dict:
    sub = ctx.sub

    if inp.op == "create":
        _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
        _require(inp.title and inp.title.strip(), "missing_title", "`title` requis.")
        _require(_can(sub, inp.project_id, "write"), "forbidden", "Écriture refusée.", 403)
        if inp.parent_id is not None:
            parent = db.get_doc_by_id(int(inp.parent_id))
            _require(parent and parent["project_id"] == inp.project_id, "bad_parent",
                     "Parent invalide (autre projet ou inexistant).")
        did = db.create_doc(int(inp.project_id), inp.title.strip(), parent_id=inp.parent_id,
                            body_md=inp.body_md or "", kind=(inp.kind or "doc"), created_by=sub)
        db.log_project_activity(int(inp.project_id), sub, "doc.create", inp.title.strip())
        return _view(db.get_doc_by_id(did))

    if inp.op == "list":
        _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
        _require(_can(sub, inp.project_id, "read"), "forbidden", "Accès refusé.", 403)
        return {"project_id": inp.project_id,
                "docs": [_view(d) for d in db.list_docs_for_project(int(inp.project_id))]}

    # ops par doc_id (résolvent le projet pour l'autz)
    _require(inp.doc_id is not None, "missing_doc", "`doc_id` requis.")
    row = db.get_doc_by_id(int(inp.doc_id))
    _require(row is not None, "unknown_doc", f"Doc #{inp.doc_id} inconnu.", 404)
    pid = row["project_id"]

    if inp.op == "get":
        _require(_can(sub, pid, "read"), "forbidden", "Accès refusé.", 403)
        return _view(row)

    if inp.op == "update":
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        db.update_doc(int(inp.doc_id), title=(inp.title.strip() if inp.title else None),
                      body_md=inp.body_md, kind=inp.kind)
        db.log_project_activity(pid, sub, "doc.update", row.get("title"))
        return _view(db.get_doc_by_id(int(inp.doc_id)))

    if inp.op == "delete":
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        db.delete_doc(int(inp.doc_id))   # CASCADE sur le sous-arbre
        db.log_project_activity(pid, sub, "doc.delete", row.get("title"))
        return {"ok": True, "id": inp.doc_id, "deleted": True}

    # move — nouveau parent dans le MÊME projet (cycle profond non gardé en v1).
    _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
    if inp.parent_id is not None:
        _require(int(inp.parent_id) != int(inp.doc_id), "bad_parent",
                 "Un doc ne peut pas être son propre parent.")
        parent = db.get_doc_by_id(int(inp.parent_id))
        _require(parent and parent["project_id"] == pid, "bad_parent",
                 "Parent invalide (autre projet ou inexistant).")
    db.move_doc(int(inp.doc_id), inp.parent_id)
    return _view(db.get_doc_by_id(int(inp.doc_id)))


CAPABILITIES += [
    Capability(
        key="me.doc", handler=_doc, Input=DocInput, authz=SUB_ONLY,
        description=(
            "Docs (markdown pages tree inside a project; inherit the project's access). "
            "op=create (project_id, title; optional parent_id/body_md/kind) / list "
            "(project_id → all pages, build the tree via parent_id) / get / update "
            "(title/body_md/kind) / delete (cascades its subtree) / move (parent_id, "
            "null=top-level). kind ∈ doc|note|source."
        ),
        mcp="oto_doc",
        rest=RestBinding("POST", "/api/me/docs"),
    ),
]
