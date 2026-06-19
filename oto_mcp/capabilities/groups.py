"""Capacités du sous-palier GROUPE (départements / équipes, ADR 0012).

CRUD groupe + groupe actif. Co-déclarées comme les capacités org (ADR 0009) :
un handler core + Input pydantic + règle d'autz (combinateurs `roles`-aware) +
bindings MCP/REST. L'autz d'écriture passe par `GROUP_ADMIN_OF` (chef d'équipe,
org_admin parent ou platform_admin par escalade) ; la création par
`ORG_ADMIN_OF` (créer un groupe = acte d'org_admin) ; les lectures par
`ORG_MEMBER_OF`/`GROUP_MEMBER_OF` ; le switch self-serve par `SUB_ONLY`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .. import group_store, org_store
from ._authz import GROUP_ADMIN_OF, GROUP_MEMBER_OF, ORG_ADMIN_OF, ORG_MEMBER_OF, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_GID = {"id": "group_id"}
_OID = {"id": "org_id"}


class CreateGroupInput(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=80)
    description: str = ""


class OrgIdInput(BaseModel):
    org_id: int


class GroupIdInput(BaseModel):
    group_id: int


class UseGroupInput(BaseModel):
    group_id: int


class UpdateGroupInput(BaseModel):
    group_id: int
    name: Optional[str] = None
    description: Optional[str] = None


def _group_brief(g: dict, sub: str) -> dict:
    members = group_store.list_group_members(g["id"])
    return {
        "id": g["id"], "group_id": g["id"], "org_id": g["org_id"],
        "name": g["name"], "description": g.get("description", ""),
        "member_count": len(members),
        "has_preset": g.get("default_tools") is not None,
        "my_role": group_store.get_group_role(g["id"], sub),
    }


def _create_group(ctx: ResolvedCtx, inp: CreateGroupInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    name = inp.name.strip()
    # Collision de nom (l'index UNIQUE (org_id, name) la rejetterait aussi, mais on
    # lève une erreur actionnable plutôt qu'une IntegrityError opaque).
    if any(g["name"].lower() == name.lower() for g in group_store.list_groups(inp.org_id)):
        raise AuthzDenied(409, "group_exists",
                          f"Un groupe `{name}` existe déjà dans cette org.")
    gid = group_store.create_group(inp.org_id, name, inp.description, created_by=ctx.sub)
    # Le créateur devient chef d'équipe du groupe (s'il est membre de l'org).
    if org_store.get_org_role(inp.org_id, ctx.sub) is not None:
        group_store.add_group_member(gid, ctx.sub, "group_admin")
    return {"id": gid, "group_id": gid, "org_id": inp.org_id, "name": inp.name.strip()}


def _list_groups(ctx: ResolvedCtx, inp: OrgIdInput) -> dict:
    out = []
    for g in group_store.list_groups(inp.org_id):
        out.append(_group_brief(g, ctx.sub))
    return {"org_id": inp.org_id, "groups": out}


class NoInput(BaseModel):
    pass


def _list_my_groups(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Groupes de l'org active du sub + son rôle + le groupe actif."""
    org_id = org_store.get_active_org(ctx.sub)
    if org_id is None:
        return {"org_id": None, "active_group": None, "groups": []}
    active_group = group_store.get_active_group(ctx.sub)
    mine = {g["group_id"]: g["group_role"] for g in
            group_store.list_groups_for_user(ctx.sub, org_id)}
    groups = []
    for g in group_store.list_groups(org_id):
        groups.append({
            "id": g["id"], "group_id": g["id"], "name": g["name"],
            "description": g.get("description", ""),
            "member_count": len(group_store.list_group_members(g["id"])),
            "my_role": mine.get(g["id"]),
            "active": g["id"] == active_group,
        })
    return {"org_id": org_id, "active_group": active_group, "groups": groups}


def _use_group(ctx: ResolvedCtx, inp: UseGroupInput) -> dict:
    g = group_store.get_group(inp.group_id)
    if not g:
        raise AuthzDenied(404, "unknown_group", f"Groupe #{inp.group_id} inconnu.")
    if not group_store.set_active_group(ctx.sub, inp.group_id):
        raise AuthzDenied(403, "not_a_member",
                          "Tu n'es pas membre de ce groupe — demande au chef d'équipe.")
    return {"active_group": inp.group_id, "name": g["name"], "active_org": g["org_id"]}


def _clear_group(ctx: ResolvedCtx, inp: NoInput) -> dict:
    group_store.clear_active_group(ctx.sub)
    return {"active_group": None}


def _group_detail(ctx: ResolvedCtx, inp: GroupIdInput) -> dict:
    g = group_store.get_group(inp.group_id)
    if not g:
        raise AuthzDenied(404, "unknown_group", f"Groupe #{inp.group_id} inconnu.")
    from .. import db
    members = []
    for m in group_store.list_group_members(inp.group_id):
        u = db.get_user(m["sub"]) or {}
        members.append({"sub": m["sub"], "email": u.get("email"), "name": u.get("name"),
                        "role": m["group_role"], "active": m["is_active"]})
    return {
        "group": _group_brief(g, ctx.sub),
        "default_tools": list(g["default_tools"]) if g.get("default_tools") is not None else None,
        "members": members,
        "secrets": group_store.list_group_secrets(inp.group_id),
    }


def _update_group(ctx: ResolvedCtx, inp: UpdateGroupInput) -> dict:
    group_store.update_group(inp.group_id, name=inp.name, description=inp.description)
    return {"ok": True, "group_id": inp.group_id}


def _delete_group(ctx: ResolvedCtx, inp: GroupIdInput) -> dict:
    deleted = group_store.delete_group(inp.group_id)
    return {"ok": True, "group_id": inp.group_id, "deleted": deleted}


CAPABILITIES += [
    Capability(
        key="group.create", handler=_create_group, Input=CreateGroupInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Create a group (department/team) inside an org you administer. "
                     "You become its team lead (group_admin)."),
        mcp="oto_create_group",
        rest=RestBinding("POST", "/api/orgs/{id}/groups", _OID),
    ),
    Capability(
        key="group.list", handler=_list_groups, Input=OrgIdInput,
        authz=ORG_MEMBER_OF("org_id"),
        description="List the groups (departments) of an org you belong to.",
        rest=RestBinding("GET", "/api/orgs/{id}/groups", _OID),
    ),
    Capability(
        key="group.list_mine", handler=_list_my_groups, Input=NoInput, authz=SUB_ONLY,
        description=("List the groups (departments) of your active org, your role in "
                     "each, and which one is active."),
        mcp="oto_list_groups",
    ),
    Capability(
        key="group.use", handler=_use_group, Input=UseGroupInput, authz=SUB_ONLY,
        description=("Switch your active group (department) by id. Sets your active org "
                     "to its parent. The active group decides which group doctrine, "
                     "toolset preset and shared secrets apply to your session."),
        mcp="oto_use_group",
        rest=RestBinding("PUT", "/api/me/active-group"),
        refresh_visibility=True,  # le groupe actif raffine la baseline de toolset (ADR 0012)
    ),
    Capability(
        key="group.clear", handler=_clear_group, Input=NoInput, authz=SUB_ONLY,
        description="Deselect your active group (operate at the org level again).",
        mcp="oto_clear_group",
        rest=RestBinding("DELETE", "/api/me/active-group"),
        refresh_visibility=True,  # retour au niveau org → baseline de l'org
    ),
    Capability(
        key="group.get", handler=_group_detail, Input=GroupIdInput,
        authz=GROUP_MEMBER_OF("group_id"),
        description="Group detail (members, shared secrets, toolset preset).",
        rest=RestBinding("GET", "/api/groups/{id}", _GID),
    ),
    Capability(
        key="group.update", handler=_update_group, Input=UpdateGroupInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Rename / re-describe a group you lead.",
        rest=RestBinding("PATCH", "/api/groups/{id}", _GID),
    ),
    Capability(
        key="group.delete", handler=_delete_group, Input=GroupIdInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Delete a group you lead (members/doctrine/secrets purged).",
        rest=RestBinding("DELETE", "/api/groups/{id}", _GID),
    ),
]
