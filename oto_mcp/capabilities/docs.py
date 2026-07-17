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


def _public_doc_url(token: str) -> str:
    """Lien public d'un doc partagé (gap #4a) — pointe sur la route publique du
    dashboard qui rend le markdown. Base configurable (défaut prod)."""
    import os
    base = os.environ.get("OTO_DASHBOARD_BASE_URL", "https://dashboard.oto.ninja").rstrip("/")
    return f"{base}/p/d/{token}"


class DocInput(BaseModel):
    op: Literal["create", "list", "search", "get", "update", "delete", "move", "revisions",
                "request_change", "list_changes", "resolve_change", "set_public"]
    project_id: Optional[int] = None   # create / list / search
    doc_id: Optional[int] = None       # get / update / delete / move / request_change / list_changes
    query: Optional[str] = None        # search : termes recherchés dans titre + corps
    parent_id: Optional[int] = None    # create / move (None = 1er niveau sous le projet)
    title: Optional[str] = None
    body_md: Optional[str] = None
    kind: Optional[Literal["doc", "note", "source"]] = None
    description: Optional[str] = None  # chapô (Ship 2) — '' efface (fallback dérivé)
    position: Optional[int] = None     # move : INDEX cible (0-based) dans la fratrie
    request_id: Optional[int] = None   # resolve_change
    message: Optional[str] = None      # request_change : note libre du demandeur
    accept: Optional[bool] = None      # resolve_change : True = accepter (applique), False = refuser
    public: Optional[bool] = None      # set_public : True = partager publiquement, False = retirer


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _can(sub: str, project_id: int, want: str) -> bool:
    return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), want)


def _view(row: dict) -> dict:
    out = {k: row.get(k) for k in
           ("id", "project_id", "parent_id", "title", "description", "position",
            "body_md", "kind", "created_at", "updated_at")}
    tok = row.get("public_token")
    out["public"] = bool(tok)
    out["public_url"] = _public_doc_url(tok) if tok else None
    return out


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
                            body_md=inp.body_md or "", kind=(inp.kind or "doc"), created_by=sub,
                            description=inp.description)
        db.log_project_activity(int(inp.project_id), sub, "doc.create", inp.title.strip())
        return _view(db.get_doc_by_id(did))

    if inp.op == "list":
        _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
        _require(_can(sub, inp.project_id, "read"), "forbidden", "Accès refusé.", 403)
        return {"project_id": inp.project_id,
                "docs": [_view(d) for d in db.list_docs_for_project(int(inp.project_id))]}

    if inp.op == "search":
        # DÉPRÉCIÉ (lot 3 Ship 1) : rerouté sur le chemin UNIQUE de recherche
        # (`oto_search` scope=project kinds=page) — un seul verbe, un seul code.
        # Forme de sortie conservée-approchée (`results`), + le pointeur.
        _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
        _require(inp.query and inp.query.strip(), "missing_query", "`query` requis.")
        _require(_can(sub, inp.project_id, "read"), "forbidden", "Accès refusé.", 403)
        from .. import search as search_mod
        out = search_mod.search(sub, ctx.org_id, inp.query.strip(),
                                scope="project", project_id=int(inp.project_id),
                                kinds=["page"])
        return {"project_id": inp.project_id, "query": inp.query.strip(),
                "deprecated": "utilise oto_search (scope=project) — même chemin, toutes sources",
                "results": [{"id": h["ref"], "project_id": h.get("project_id"),
                             "title": h["title"], "snippet": h.get("passage") or "",
                             "updated_at": h.get("updated_at")} for h in out["hits"]]}

    # ops par doc_id (résolvent le projet pour l'autz)
    _require(inp.doc_id is not None, "missing_doc", "`doc_id` requis.")
    row = db.get_doc_by_id(int(inp.doc_id))
    _require(row is not None, "unknown_doc", f"Doc #{inp.doc_id} inconnu.", 404)
    pid = row["project_id"]

    if inp.op == "get":
        _require(_can(sub, pid, "read"), "forbidden", "Accès refusé.", 403)
        return _view(row)

    if inp.op == "revisions":
        _require(_can(sub, pid, "read"), "forbidden", "Accès refusé.", 403)
        return {"doc_id": inp.doc_id,
                "revisions": db.list_doc_revisions(int(inp.doc_id))}

    if inp.op == "set_public":
        # Partager publiquement (ou retirer) — action d'écriture (gap #4a).
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        token = db.set_doc_public(int(inp.doc_id), bool(inp.public))
        db.log_project_activity(pid, sub, "doc.set_public",
                                f"{row.get('title')}:{bool(inp.public)}")
        return {"ok": True, "id": inp.doc_id, "public": bool(token),
                "public_url": _public_doc_url(token) if token else None}

    if inp.op == "request_change":
        # Lecture seule → propose ; il faut au moins l'accès LECTURE au projet.
        _require(_can(sub, pid, "read"), "forbidden", "Accès refusé.", 403)
        body = inp.body_md if inp.body_md is not None else row.get("body_md", "")
        req = db.add_doc_change_request(
            int(inp.doc_id), sub,
            proposed_title=(inp.title.strip() if inp.title else None),
            proposed_body_md=body, message=inp.message)
        db.log_project_activity(pid, sub, "doc.change_request", row.get("title"))
        return {"ok": True, "request": req}

    if inp.op == "list_changes":
        # Réservé à qui peut trancher (write) : voir les demandes en attente.
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        return {"doc_id": inp.doc_id,
                "requests": db.list_doc_change_requests(int(inp.doc_id))}

    if inp.op == "resolve_change":
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        _require(inp.request_id is not None, "missing_request", "`request_id` requis.")
        cr = db.get_doc_change_request(int(inp.request_id))
        _require(cr is not None and cr["doc_id"] == int(inp.doc_id), "unknown_request",
                 "Demande inconnue (ou autre doc).", 404)
        _require(cr["status"] == "pending", "already_resolved", "Demande déjà traitée.")
        if inp.accept:
            # Applique le contenu proposé (update_doc snapshotte la version courante, B4c).
            db.update_doc(int(inp.doc_id),
                          title=(cr.get("proposed_title") or None),
                          body_md=cr.get("proposed_body_md"), edited_by=sub)
            db.resolve_doc_change_request(int(inp.request_id), "accepted", sub)
            db.log_project_activity(pid, sub, "doc.change_accepted", row.get("title"))
        else:
            db.resolve_doc_change_request(int(inp.request_id), "rejected", sub)
            db.log_project_activity(pid, sub, "doc.change_rejected", row.get("title"))
        return {"ok": True, "id": inp.request_id, "accepted": bool(inp.accept)}

    if inp.op == "update":
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        db.update_doc(int(inp.doc_id), title=(inp.title.strip() if inp.title else None),
                      body_md=inp.body_md, kind=inp.kind, edited_by=sub,
                      description=inp.description)
        db.log_project_activity(pid, sub, "doc.update", row.get("title"))
        return _view(db.get_doc_by_id(int(inp.doc_id)))

    if inp.op == "delete":
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        db.delete_doc(int(inp.doc_id))   # CASCADE sur le sous-arbre
        db.log_project_activity(pid, sub, "doc.delete", row.get("title"))
        return {"ok": True, "id": inp.doc_id, "deleted": True}

    # move — nouveau parent dans le MÊME projet (cycle profond non gardé en v1) ET/OU
    # réordonnancement (Ship 2 : `position` = index cible, la fratrie est réindexée).
    _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
    if inp.parent_id is not None:
        _require(int(inp.parent_id) != int(inp.doc_id), "bad_parent",
                 "Un doc ne peut pas être son propre parent.")
        parent = db.get_doc_by_id(int(inp.parent_id))
        _require(parent and parent["project_id"] == pid, "bad_parent",
                 "Parent invalide (autre projet ou inexistant).")
    # `parent_id` absent + `position` posé = réordonner DANS la fratrie courante.
    target_parent = inp.parent_id if inp.parent_id is not None else (
        row.get("parent_id") if inp.position is not None else None)
    db.move_doc(int(inp.doc_id), target_parent, position=inp.position)
    return _view(db.get_doc_by_id(int(inp.doc_id)))


CAPABILITIES += [
    Capability(
        key="me.doc", handler=_doc, Input=DocInput, authz=SUB_ONLY,
        description=(
            "Docs (markdown pages tree inside a project; inherit the project's access). "
            "**This is also the org KNOWLEDGE BASE**: resolve it with oto_kb → project_id, "
            "then read/search/write reference pages here (the dashboard « Documents » zone). "
            "Prefer it over the web for org facts (processes, context, conventions), and "
            "CAPTURE durable, sourced facts here (kind=source/note) as you learn them. "
            "op=create (project_id, title; optional parent_id/body_md/kind) / list "
            "(project_id → all pages, build the tree via parent_id) / search (project_id + "
            "query → full-text hits {id,title,kind,snippet}: LOCATE a page, then get its "
            "content) / get / update (title/body_md/kind ; snapshots the prior version) / "
            "revisions (doc_id → version history, newest first) / request_change (read-only "
            "users propose a new body_md/title + message) / list_changes (owner: pending "
            "requests) / resolve_change (request_id + accept: true applies it, false rejects) "
            "/ set_public (public: true → shareable public read-only link, false → private ; "
            "returns public_url) / delete (cascades its subtree) / move (parent_id, "
            "null=top-level). kind ∈ doc|note|source."
        ),
        mcp="oto_doc",
        rest=RestBinding("POST", "/api/me/docs"),
    ),
]
