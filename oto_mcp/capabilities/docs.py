"""Doc — page markdown arborescente d'un projet (incrément 3, modèle produit 2026-06-27).

Un Doc appartient à un projet et **hérite de son accès** (`ownership.can_access` sur le
projet — pas d'ownership propre). Le `brief_md` du projet reste la page d'entrée ; les
Docs sont les pages, en arbre via `parent_id`. kind ∈ {doc (humain), note (agent),
source (import)}. CRUD + move, co-déclaré MCP+REST.
"""
from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, doc_patch, email, org_store, ownership
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

PROJECT_RTYPE = "project"


def _dash_url() -> str:
    return os.environ.get("OTO_DASHBOARD_BASE_URL", "https://dashboard.oto.ninja").rstrip("/")


def _email_of(sub: Optional[str]) -> Optional[str]:
    if not sub:
        return None
    return (db.get_user(sub) or {}).get("email")


def _notify_cr_created(pid: int, proposer_sub: str, *, is_create: bool,
                       doc_title: Optional[str]) -> None:
    """Prévient les VALIDATEURS qu'une proposition attend (oto/#6, « les auteurs
    valident »). Destinataires = org_admins de l'org du projet + le propriétaire si le
    projet est user-owned, SAUF le proposeur. Best-effort — ne casse jamais la création."""
    try:
        project = db.get_project_by_id(int(pid)) or {}
        pname = project.get("name")
        org = project.get("context_org_id")
        recips: set[str] = set()
        if org is not None:
            for m in org_store.list_org_members(int(org)):
                if m.get("org_role") == "org_admin" and m.get("sub") != proposer_sub:
                    if e := _email_of(m.get("sub")):
                        recips.add(e)
        if project.get("owner_type") == "user" and project.get("owner_id") != proposer_sub:
            if e := _email_of(project.get("owner_id")):
                recips.add(e)
        if not recips:
            return
        proposer = (db.get_user(proposer_sub) or {}).get("name") or (db.get_user(proposer_sub) or {}).get("email")
        url = f"{_dash_url()}/projects/{int(pid)}"
        for to in recips:
            email.send_change_request_email(
                to, project_name=pname, doc_title=doc_title, proposer=proposer,
                is_create=is_create, app_url=url)
    except Exception as e:  # best-effort
        logger.warning("notify CR created (project %s) failed: %s", pid, e)


def _notify_cr_resolved(cr: dict, accepted: bool) -> None:
    """Prévient le PROPOSEUR que sa proposition a été tranchée (oto/#6). Best-effort."""
    try:
        to = _email_of(cr.get("requested_by"))
        if not to:
            return
        pname = cr.get("project_name")
        pid = cr.get("project_id") or (cr.get("doc_id") and (db.get_doc_by_id(int(cr["doc_id"])) or {}).get("project_id"))
        url = f"{_dash_url()}/projects/{int(pid)}" if pid else _dash_url()
        email.send_change_request_resolved_email(
            to, project_name=pname, doc_title=cr.get("doc_title"), accepted=accepted, app_url=url)
    except Exception as e:  # best-effort
        logger.warning("notify CR resolved (#%s) failed: %s", cr.get("id"), e)


def _public_doc_url(token: str) -> str:
    """Lien public d'un doc partagé (gap #4a) — pointe sur la route publique du
    dashboard qui rend le markdown. Base configurable (défaut prod)."""
    import os
    base = os.environ.get("OTO_DASHBOARD_BASE_URL", "https://dashboard.oto.ninja").rstrip("/")
    return f"{base}/p/d/{token}"


class DocInput(BaseModel):
    op: Literal["create", "list", "search", "get", "update", "patch", "delete", "move",
                "revisions", "request_change", "list_changes", "resolve_change",
                "set_public", "backlinks"]
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
    expected_rev: Optional[str] = None  # update/patch : rev (ETag) lue par le client → conflit optimiste
    section: Optional[str] = None       # patch : titre (heading markdown) de la section ciblée
    mode: Optional[Literal["replace", "append", "prepend"]] = None  # patch : défaut replace


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _can(sub: str, project_id: int, want: str) -> bool:
    return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), want)


def _view(row: dict) -> dict:
    out = {k: row.get(k) for k in
           ("id", "project_id", "parent_id", "title", "description", "position",
            "body_md", "kind", "created_at", "updated_at")}
    # rev = ETag de contenu : à relire par le client et repasser en `expected_rev`
    # sur op=update pour détecter un écrasement concurrent (oto/#6).
    out["rev"] = db.doc_rev(row.get("title"), row.get("body_md"))
    tok = row.get("public_token")
    out["public"] = bool(tok)
    out["public_url"] = _public_doc_url(tok) if tok else None
    return out


def _doc(ctx: ResolvedCtx, inp: DocInput) -> dict:
    sub = ctx.sub

    if inp.op == "create":
        _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
        _require(inp.title and inp.title.strip(), "missing_title", "`title` requis.")
        # « Les lecteurs proposent » (Ship 3) : un viewer (lecture SANS écriture) qui
        # crée obtient une PROPOSITION de création, pas la page.
        if not _can(sub, inp.project_id, "write"):
            _require(_can(sub, inp.project_id, "read"), "forbidden", "Accès refusé.", 403)
            req = db.add_doc_change_request(
                sub, project_id=int(inp.project_id), proposed_parent_id=inp.parent_id,
                proposed_kind=(inp.kind or "doc"),
                proposed_title=inp.title.strip(), proposed_body_md=inp.body_md or "",
                message=inp.message)
            _notify_cr_created(int(inp.project_id), sub, is_create=True, doc_title=None)
            return {"status": "proposal_created", "request": req}
        if inp.parent_id is not None:
            parent = db.get_doc_by_id(int(inp.parent_id))
            _require(parent and parent["project_id"] == inp.project_id, "bad_parent",
                     "Parent invalide (autre projet ou inexistant).")
        did = db.create_doc(int(inp.project_id), inp.title.strip(), parent_id=inp.parent_id,
                            body_md=inp.body_md or "", kind=(inp.kind or "doc"), created_by=sub,
                            description=inp.description)
        db.log_project_activity(int(inp.project_id), sub, "doc.create", inp.title.strip())
        return _view(db.get_doc_by_id(did))

    # ── Propositions (Ship 3) — AVANT le gate doc_id : une proposition de CRÉATION a
    # doc_id=NULL, elle serait inatteignable sinon. On résout le projet par request_id
    # (resolve) / project_id (create-proposal, list) / doc_id (modif, legacy).
    if inp.op == "resolve_change":
        _require(inp.request_id is not None, "missing_request", "`request_id` requis.")
        cr = db.get_doc_change_request(int(inp.request_id))
        _require(cr is not None, "unknown_request", "Demande inconnue.", 404)
        _require(cr["status"] == "pending", "already_resolved", "Demande déjà traitée.")
        cr_pid = cr.get("project_id") or (
            (db.get_doc_by_id(int(cr["doc_id"])) or {}).get("project_id") if cr.get("doc_id") else None)
        _require(cr_pid is not None, "unknown_request", "Cible de la demande introuvable.", 404)
        _require(_can(sub, cr_pid, "write"), "forbidden", "Écriture refusée.", 403)
        if inp.accept:
            if cr.get("doc_id"):
                # MODIF : la page cible existe-t-elle encore ? sinon on ferme (motif).
                if db.get_doc_by_id(int(cr["doc_id"])) is None:
                    db.resolve_doc_change_request(int(inp.request_id), "rejected", sub)
                    _notify_cr_resolved(cr, False)
                    return {"ok": True, "id": inp.request_id, "accepted": False,
                            "reason": "page supprimée"}
                db.update_doc(int(cr["doc_id"]),
                              title=(cr.get("proposed_title") or None),
                              body_md=cr.get("proposed_body_md"), edited_by=sub)
            else:
                # CRÉATION : parent supprimé entre-temps → rattache à la racine (mention).
                parent = cr.get("proposed_parent_id")
                if parent is not None and db.get_doc_by_id(int(parent)) is None:
                    parent = None
                db.create_doc(int(cr_pid), (cr.get("proposed_title") or "Sans titre"),
                              parent_id=parent, body_md=cr.get("proposed_body_md") or "",
                              kind=(cr.get("proposed_kind") or "doc"), created_by=sub)
            db.resolve_doc_change_request(int(inp.request_id), "accepted", sub)
            db.log_project_activity(int(cr_pid), sub, "doc.change_accepted", cr.get("proposed_title"))
        else:
            db.resolve_doc_change_request(int(inp.request_id), "rejected", sub)
            db.log_project_activity(int(cr_pid), sub, "doc.change_rejected", cr.get("proposed_title"))
        _notify_cr_resolved(cr, bool(inp.accept))
        return {"ok": True, "id": inp.request_id, "accepted": bool(inp.accept)}

    if inp.op == "list_changes" and inp.project_id is not None:
        # Toutes les propositions en attente d'un PROJET (drawer « Propositions (N) »).
        _require(_can(sub, inp.project_id, "write"), "forbidden", "Écriture refusée.", 403)
        return {"project_id": inp.project_id,
                "requests": db.list_change_requests_by_project([int(inp.project_id)])}

    if inp.op == "request_change" and inp.doc_id is None:
        # Proposition de CRÉATION (viewer) : project_id + emplacement proposé.
        _require(inp.project_id is not None, "missing_project", "`project_id` ou `doc_id` requis.")
        _require(inp.title and inp.title.strip(), "missing_title", "`title` requis.")
        _require(_can(sub, inp.project_id, "read"), "forbidden", "Accès refusé.", 403)
        req = db.add_doc_change_request(
            sub, project_id=int(inp.project_id), proposed_parent_id=inp.parent_id,
            proposed_kind=(inp.kind or "doc"),
            proposed_title=inp.title.strip(), proposed_body_md=inp.body_md or "",
            message=inp.message)
        _notify_cr_created(int(inp.project_id), sub, is_create=True, doc_title=None)
        return {"ok": True, "request": req}

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

    if inp.op == "backlinks":
        # « Cité par » (Ship 4) : les pages qui mentionnent celle-ci via [[…]],
        # FILTRÉES par accès (une page d'un projet non lisible ne fuite pas).
        _require(_can(sub, pid, "read"), "forbidden", "Accès refusé.", 403)
        seen: dict[int, bool] = {}
        def _readable(prj: int) -> bool:
            if prj not in seen:
                seen[prj] = _can(sub, prj, "read")
            return seen[prj]
        cites = [b for b in db.doc_backlinks(int(inp.doc_id)) if _readable(b["project_id"])]
        return {"doc_id": inp.doc_id, "backlinks": cites, "count": len(cites)}

    if inp.op == "set_public":
        # Partager publiquement (ou retirer) — action d'écriture (gap #4a).
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        token = db.set_doc_public(int(inp.doc_id), bool(inp.public))
        db.log_project_activity(pid, sub, "doc.set_public",
                                f"{row.get('title')}:{bool(inp.public)}")
        return {"ok": True, "id": inp.doc_id, "public": bool(token),
                "public_url": _public_doc_url(token) if token else None}

    if inp.op == "request_change":
        # MODIF (doc_id) — lecture seule → propose ; ≥ accès LECTURE au projet.
        _require(_can(sub, pid, "read"), "forbidden", "Accès refusé.", 403)
        body = inp.body_md if inp.body_md is not None else row.get("body_md", "")
        req = db.add_doc_change_request(
            sub, doc_id=int(inp.doc_id),
            proposed_title=(inp.title.strip() if inp.title else None),
            proposed_body_md=body, message=inp.message)
        db.log_project_activity(pid, sub, "doc.change_request", row.get("title"))
        _notify_cr_created(int(pid), sub, is_create=False, doc_title=row.get("title"))
        return {"ok": True, "request": req}

    if inp.op == "list_changes":
        # Par doc (legacy — la voie par projet est gérée avant le gate).
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        return {"doc_id": inp.doc_id,
                "requests": db.list_doc_change_requests(int(inp.doc_id))}

    if inp.op == "update":
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        try:
            db.update_doc(int(inp.doc_id), title=(inp.title.strip() if inp.title else None),
                          body_md=inp.body_md, kind=inp.kind, edited_by=sub,
                          description=inp.description, expected_rev=inp.expected_rev)
        except db.DocConflict as e:
            # Écrasement concurrent évité : le doc a changé depuis la lecture du client.
            _require(False, "conflict",
                     f"Le doc a été modifié entre-temps (rev actuelle {e.current_rev}). "
                     f"Relis-le (op=get) et refais ton édition sur la version à jour.", 409)
        db.log_project_activity(pid, sub, "doc.update", row.get("title"))
        return _view(db.get_doc_by_id(int(inp.doc_id)))

    if inp.op == "patch":
        # Édition PARTIELLE par section (top5 #3) : ne touche QUE la section `section`
        # (titre markdown) → deux auteurs sur des sections différentes ne s'écrasent
        # plus. On applique le patch puis on réécrit via update_doc (révisions +
        # backlinks + conflit optimiste conservés).
        _require(_can(sub, pid, "write"), "forbidden", "Écriture refusée.", 403)
        _require(inp.section and inp.section.strip(), "missing_section",
                 "`section` (titre de la section à modifier) requis.")
        _require(inp.body_md is not None, "missing_body", "`body_md` (nouveau contenu) requis.")
        try:
            new_body = doc_patch.patch_section(
                row.get("body_md") or "", inp.section, inp.body_md, mode=(inp.mode or "replace"))
        except doc_patch.SectionNotFound as e:
            _require(False, "unknown_section",
                     f"Section « {inp.section} » introuvable. Sections disponibles : "
                     f"{', '.join(e.available) or '(aucune)'}.", 404)
        try:
            db.update_doc(int(inp.doc_id), body_md=new_body, edited_by=sub,
                          expected_rev=inp.expected_rev)
        except db.DocConflict as e:
            _require(False, "conflict",
                     f"Le doc a été modifié entre-temps (rev actuelle {e.current_rev}). "
                     f"Relis-le (op=get) et refais ton patch sur la version à jour.", 409)
        db.log_project_activity(pid, sub, "doc.patch", f"{row.get('title')} § {inp.section}")
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
    # Trois intentions distinguées par `model_fields_set` (JSON null ≠ absent) :
    # parent FOURNI (id ou null=racine) = reparenter là ; absent + `position` posé =
    # réordonner DANS la fratrie courante ; absent + rien = racine (historique).
    if "parent_id" in inp.model_fields_set:
        target_parent = inp.parent_id
    elif inp.position is not None:
        target_parent = row.get("parent_id")
    else:
        target_parent = None
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
            "content) / get (returns `rev`, an ETag) / update (title/body_md/kind, full body; "
            "snapshots the prior version; pass `expected_rev` from op=get for optimistic "
            "conflict detection → 409 if the page changed since) / patch (edit ONE section in "
            "place: `section`=its markdown heading + `body_md` + `mode` replace|append|prepend "
            "→ two authors on different sections don't clobber; also honours `expected_rev`) / "
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
