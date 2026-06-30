"""Projet — couche d'organisation (modèle produit 2026-06-27 ; owned resource ADR 0030).

Un **Projet** = un conteneur de travail POSSÉDÉ (owner_type/owner_id) : un nom + un
**brief** (le doc d'entrée, inline pour l'instant). CRUD co-déclaré MCP+REST (ADR 0009).
L'accès dérive du seam `ownership` : `can_access` (contenu, owner ∪ grants) pour
lire/écrire, `can_govern` (owner ∪ escalade `roles.py`) pour archiver.

Hors périmètre de cet incrément (suivants) : le **partage / transfert** (capacité
générique `oto_resource`, resource_type='project' déjà enregistré dans `ownership`),
les **liens** vers tableaux/procédures/connecteurs/bases, et le **Doc arborescent**
(le brief devient alors le Doc racine).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, ownership, roles
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

RTYPE = "project"


_LINK_TYPES = ("tableau", "procedure", "connecteur", "base")


class ProjectInput(BaseModel):
    op: Literal["create", "list", "get", "update", "archive", "link", "unlink", "activity"]
    project_id: Optional[int] = None
    name: Optional[str] = None
    brief_md: Optional[str] = None
    # create : owner du projet — 'user' (défaut, perso) ou 'org' (classeur d'équipe).
    owner_type: Literal["user", "org"] = "user"
    owner_id: Optional[str] = None   # org.id si owner_type='org' ; ignoré pour 'user'
    # link / unlink : un pointeur typé vers une entité regroupée par le projet.
    target_type: Optional[Literal["tableau", "procedure", "connecteur", "base"]] = None
    target_ref: Optional[str] = None   # datastore.id | doctrine slug | connecteur name | base id
    label: Optional[str] = None        # nom d'affichage (link)


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _view(row: dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "brief_md": row.get("brief_md", ""),
        "owner_type": row["owner_type"], "owner_id": row["owner_id"],
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "archived_at": row.get("archived_at"),
    }


def _project(ctx: ResolvedCtx, inp: ProjectInput) -> dict:
    sub = ctx.sub

    if inp.op == "create":
        _require(inp.name and inp.name.strip(), "missing_name", "`name` requis.")
        if inp.owner_type == "org":
            _require(inp.owner_id, "missing_owner",
                     "`owner_id` (org) requis pour un projet d'org.")
            _require(roles.is_org_member(sub, int(inp.owner_id)), "forbidden",
                     "Tu n'es pas membre de cette org.", 403)
            owner_type, owner_id = "org", str(inp.owner_id)
        else:
            # Défaut = org ACTIVE de l'user (plus de perso ; ctx.org_id toujours posé).
            _require(ctx.org_id is not None, "no_active_org", "Aucune org active.", 400)
            owner_type, owner_id = "org", str(ctx.org_id)
        pid = db.create_project(owner_type, owner_id, inp.name.strip(),
                                inp.brief_md or "", created_by=sub)
        db.log_project_activity(pid, sub, "project.create", inp.name.strip())
        return _view(db.get_project_by_id(pid))

    if inp.op == "list":
        owners = ownership.accessor_scope(sub).owner_pairs()
        return {"projects": [_view(r) for r in db.list_projects_for_owners(owners)]}

    # ops ciblées : project_id requis + existence
    _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
    rid = str(inp.project_id)
    row = db.get_project_by_id(int(inp.project_id))
    _require(row is not None, "unknown_project", f"Projet #{inp.project_id} inconnu.", 404)

    if inp.op == "get":
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        return {**_view(row), "links": db.list_project_links(int(inp.project_id))}

    if inp.op == "activity":
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        return {"id": inp.project_id, "activity": db.list_project_activity(int(inp.project_id))}

    if inp.op == "update":
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        db.update_project(int(inp.project_id),
                          name=(inp.name.strip() if inp.name else None),
                          brief_md=inp.brief_md)
        db.log_project_activity(int(inp.project_id), sub, "project.update", inp.name or None)
        return _view(db.get_project_by_id(int(inp.project_id)))

    if inp.op in ("link", "unlink"):
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        _require(inp.target_type and inp.target_ref, "missing_target",
                 "`target_type` et `target_ref` requis.")
        if inp.op == "link":
            db.add_project_link(int(inp.project_id), inp.target_type, inp.target_ref, inp.label)
        else:
            db.remove_project_link(int(inp.project_id), inp.target_type, inp.target_ref)
        db.log_project_activity(int(inp.project_id), sub, f"project.{inp.op}",
                                f"{inp.target_type}:{inp.label or inp.target_ref}")
        return {"ok": True, "id": inp.project_id,
                "links": db.list_project_links(int(inp.project_id))}

    # archive
    _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
             "Archivage réservé au propriétaire / admin.", 403)
    db.archive_project(int(inp.project_id))
    db.log_project_activity(int(inp.project_id), sub, "project.archive", row.get("name"))
    return {"ok": True, "id": inp.project_id, "archived": True}


CAPABILITIES += [
    Capability(
        key="me.project", handler=_project, Input=ProjectInput, authz=SUB_ONLY,
        description=(
            "Projects (organization layer, ADR 0030 owned resource). op=create (name, "
            "optional brief_md; owner_type user|org + owner_id for a team project) / list "
            "(yours + your orgs') / get (project + its links) / update (name, brief_md) / "
            "archive / link & unlink (attach an entity: target_type tableau|procedure|"
            "connecteur|base + target_ref = its id/slug/name, optional label). Share & "
            "transfer go through oto_resource (resource_type='project')."
        ),
        mcp="oto_project",
        rest=RestBinding("POST", "/api/me/projects"),
    ),
]
