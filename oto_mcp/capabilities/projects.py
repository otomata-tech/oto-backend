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

from .. import db, ownership, roles, session_org
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

RTYPE = "project"


_LINK_TYPES = ("tableau", "procedure", "connecteur", "base")


class ProjectInput(BaseModel):
    op: Literal["create", "list", "list_templates", "get", "update", "archive",
                "copy", "handoff", "link", "unlink", "activity"]
    project_id: Optional[int] = None
    name: Optional[str] = None
    brief_md: Optional[str] = None
    is_template: Optional[bool] = None   # update : publier/retirer le projet comme MODÈLE (ADR 0032 §7 B5a)
    # create : owner du projet — 'user' (défaut, perso) ou 'org' (classeur d'équipe).
    owner_type: Literal["user", "org"] = "user"
    owner_id: Optional[str] = None   # org.id si owner_type='org' ; ignoré pour 'user'
    # link / unlink : un pointeur typé vers une entité regroupée par le projet.
    target_type: Optional[Literal["tableau", "procedure", "connecteur", "base"]] = None
    target_ref: Optional[str] = None   # datastore.id | doctrine slug | connecteur name | base id
    label: Optional[str] = None        # nom d'affichage (link)
    role: Optional[str] = None         # pourquoi cette entité est ici / son rôle dans le projet (ADR 0032 §2)
    config: Optional[dict] = None      # surcharge contextuelle PRÉFAITE du lien (ADR 0032 §4) — connecteur : {identity_id?, instructions_md?}


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _handoff_md(row: dict) -> str:
    """Texte copier-coller « reprendre dans Claude » (ADR 0032 §7 B5b) : un blob
    universel (Claude/GPT/markdown) qui pré-écrit « charge ce projet ». Pur (entrée
    = dict projet, sortie = str), sans I/O — testable isolément.

    SÉCURITÉ — n'embarque PAS le `brief_md` : un projet partagé/modèle peut porter un
    brief à contenu hostile (injection de prompt) qui, collé dans Claude, s'exécuterait
    comme une consigne. Le blob ne porte que l'instruction de CHARGEMENT (id + nom) ;
    l'agent lit le brief via `oto_project(op=get)` — donnée d'outil, pas texte pré-collé."""
    pid, name = row["id"], row.get("name") or f"#{row['id']}"
    return (
        f"Charge le projet Oto #{pid} « {name} » : appelle `oto_use_project({pid})` "
        f"pour l'activer dans cette conversation, puis `oto_project(op=get, "
        f"project_id={pid})` pour son brief, ses pages et ses entités liées. "
        f"Travaille DANS ce projet (ses connecteurs préconfigurés, ses tableaux de sortie)."
    )


def _view(row: dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "brief_md": row.get("brief_md", ""),
        "owner_type": row["owner_type"], "owner_id": row["owner_id"],
        "is_template": bool(row.get("is_template")),
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
        # Scopé à l'org active (seam `ownership.active_owner`) : charger une org ne
        # montre QUE ses projets (l'org est le contexte, ADR 0023). Un projet d'une
        # autre org ne fuite plus.
        owner = ownership.active_owner(ctx.org_id)
        _require(owner is not None, "no_active_org", "Aucune org active.", 400)
        return {"projects": [_view(r) for r in db.list_projects_for_owners([owner])]}

    if inp.op == "list_templates":
        # Modèles (is_template) lisibles par l'acteur — la bibliothèque copiable (B5a).
        owners = ownership.accessor_scope(sub).owner_pairs()
        return {"projects": [_view(r) for r in
                             db.list_projects_for_owners(owners, templates_only=True)]}

    # ops ciblées : project_id requis + existence
    _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
    rid = str(inp.project_id)
    row = db.get_project_by_id(int(inp.project_id))
    _require(row is not None, "unknown_project", f"Projet #{inp.project_id} inconnu.", 404)

    if inp.op == "get":
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        # Part publique CHIFFRÉE (ADR 0032 §3) : le serveur ne connaît que sa présence
        # + son horodatage, JAMAIS la clé (côté navigateur). La (re)publication passe
        # par la route REST dédiée (le ciphertext vient du front, pas de l'agent).
        share = db.get_project_public_share(int(inp.project_id))
        return {**_view(row),
                "can_write": ownership.can_access(sub, RTYPE, rid, "write"),
                "public_shared": bool(share),
                "public_shared_at": share.get("updated_at") if share else None,
                "links": db.list_project_links(int(inp.project_id))}

    if inp.op == "activity":
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        return {"id": inp.project_id, "activity": db.list_project_activity(int(inp.project_id))}

    if inp.op == "handoff":
        # « Reprendre dans Claude » (B5b) : blob copier-coller qui charge ce projet.
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        return {"id": inp.project_id, "markdown": _handoff_md(row)}

    if inp.op == "update":
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        # Publier/retirer comme MODÈLE est un acte de gouvernance (visible aux autres
        # membres de l'org comme bibliothèque) → can_govern, pas un simple write.
        if inp.is_template is not None:
            _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
                     "Publier un modèle est réservé au propriétaire / admin.", 403)
        db.update_project(int(inp.project_id),
                          name=(inp.name.strip() if inp.name else None),
                          brief_md=inp.brief_md, is_template=inp.is_template)
        db.log_project_activity(int(inp.project_id), sub, "project.update", inp.name or None)
        return _view(db.get_project_by_id(int(inp.project_id)))

    if inp.op == "copy":
        # Copier un projet qu'on peut LIRE (le sien ou un modèle) → nouveau projet
        # possédé par l'org active (ADR 0032 §7 B5a). L'original reste intact.
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        _require(inp.name and inp.name.strip(), "missing_name", "`name` (cible) requis.")
        _require(ctx.org_id is not None, "no_active_org", "Aucune org active.", 400)
        new_id = db.duplicate_project(int(inp.project_id), inp.name.strip(),
                                      "org", str(ctx.org_id), copied_by=sub)
        return {**_view(db.get_project_by_id(new_id)),
                "links": db.list_project_links(new_id), "copied_from": inp.project_id}

    if inp.op in ("link", "unlink"):
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        _require(inp.target_type and inp.target_ref, "missing_target",
                 "`target_type` et `target_ref` requis.")
        if inp.op == "link":
            db.add_project_link(int(inp.project_id), inp.target_type, inp.target_ref,
                                inp.label, role=inp.role, config=inp.config)
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
            "(yours + your orgs') / list_templates (published MODEL projects you can copy) / "
            "get (project + its links) / update (name, brief_md, is_template = publish/unpublish "
            "as a copyable model) / copy (deep-copy a project you can read — its own or a model "
            "— into a NEW project in your active org: brief + doc tree + links + raw files; "
            "datastore rows are NOT duplicated, tableau links stay pointers; pass project_id "
            "= source + name = target) / handoff (a copy-paste « resume in Claude » blob "
            "that pre-writes oto_use_project for this project) / archive / link & unlink "
            "(attach an entity: "
            "target_type tableau|procedure|"
            "connecteur|base + target_ref = its id/slug/name, optional label + optional "
            "role = why this entity belongs to the project + optional config = the entity's "
            "PRE-MADE per-project override; for a connecteur: {identity_id?, instructions_md?} "
            "= which account to act as + prose instructions to apply (e.g. 'only filter "
            "agreements by the mutuelle theme'). Re-linking without role/config preserves the "
            "existing ones. get/link return each link's role + config + a derived "
            "`cross_project` flag (the same entity is linked by another project → avoid brutal "
            "edits / ask). Share & transfer go through oto_resource (resource_type='project')."
        ),
        mcp="oto_project",
        rest=RestBinding("POST", "/api/me/projects"),
    ),
]


# ── Bracelet de session « projet actif » (ADR 0032 §4, B2.2) ─────────────────
# `oto_use_project` pose un projet ACTIF pour la conversation (override de session,
# éphémère, MCP-only — pas de « projet maison »). Tant qu'il est actif, la résolution
# d'identité d'un connecteur applique la surcharge PRÉFAITE du projet (le compte épinglé
# sur le lien). Le bracelet SÉLECTIONNE un projet préfait ; il ne déclare aucune config.
# Miroir de `oto_use_org` (ADR 0023), sans persistance.


class UseProjectInput(BaseModel):
    project_id: int   # id d'un projet auquel tu as accès (cf. oto_project op=list)


class NoInput(BaseModel):
    pass


def _use_project(ctx: ResolvedCtx, inp: UseProjectInput) -> dict:
    """Active un projet pour CETTE conversation (override de session, ADR 0032 §4)."""
    rid = str(inp.project_id)
    row = db.get_project_by_id(inp.project_id)
    _require(row is not None, "unknown_project", f"Projet #{inp.project_id} inconnu.", 404)
    _require(ownership.can_access(ctx.sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
    sid = session_org.current_session_id()
    _require(sid is not None, "no_session",
             "oto_use_project ne s'utilise que dans une conversation MCP.", 400)
    session_org.set_project_override(sid, inp.project_id)
    # Surcharges connecteur préfaites portées par ce projet (informatif pour l'agent).
    overrides = [{"connector": l["target_ref"], "config": l.get("config") or {}}
                 for l in db.list_project_links(inp.project_id)
                 if l.get("target_type") == "connecteur" and (l.get("config") or {})]
    return {"active_project": inp.project_id, "name": row.get("name"),
            "connector_overrides": overrides}


def _clear_project(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Quitte le projet actif de la conversation (retour « hors projet »)."""
    sid = session_org.current_session_id()
    if sid is not None:
        session_org.clear_project_override(sid)
    return {"active_project": None}


CAPABILITIES += [
    Capability(
        key="me.use_project", handler=_use_project, Input=UseProjectInput, authz=SUB_ONLY,
        description=(
            "Set the ACTIVE PROJECT for this conversation (project_id from oto_project "
            "op=list). While a project is active, connectors resolve the project's PRE-MADE "
            "identity (which account to act as), set up ahead of time on the project — you "
            "don't declare it. Ephemeral: this conversation only; a new conversation starts "
            "with no active project. Returns the project's connector overrides. Leave with "
            "oto_clear_project."
        ),
        mcp="oto_use_project",
    ),
    Capability(
        key="me.clear_project", handler=_clear_project, Input=NoInput, authz=SUB_ONLY,
        description="Leave the active project of this conversation (back to no project).",
        mcp="oto_clear_project",
    ),
]
