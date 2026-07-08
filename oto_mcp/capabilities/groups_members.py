"""Capacités d'écriture sur les membres d'un groupe (ADR 0012).

Autz = `GROUP_ADMIN_OF` (chef d'équipe, org_admin parent, ou platform_admin par
escalade `roles`). INVARIANT : on n'ajoute au groupe qu'un membre DÉJÀ dans l'org
parente (l'appartenance groupe est subordonnée à l'org). Garde « dernier chef »
au niveau handler.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import db, group_store, org_store
from ._authz import GROUP_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_GID = {"id": "group_id"}


def _resolve_target(target: str) -> str:
    target = (target or "").strip()
    if not target:
        raise AuthzDenied(400, "missing_target", "Cible (email ou sub) requise.")
    if "@" in target:
        user = db.get_user_by_email(target)
        if not user:
            raise AuthzDenied(400, "unknown_user",
                              f"Aucun user connu avec l'email `{target}`.")
        return user["sub"]
    return target


def _check_role(role: str) -> str:
    if role not in group_store.GROUP_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle de groupe invalide : {role!r}.")
    return role


class AddGroupMemberInput(BaseModel):
    group_id: int
    target: str
    role: str = "group_member"


class SetGroupMemberRoleInput(BaseModel):
    group_id: int
    sub: str
    role: str


class RemoveGroupMemberInput(BaseModel):
    group_id: int
    target: str


def _add_member(ctx: ResolvedCtx, inp: AddGroupMemberInput) -> dict:
    role = _check_role(inp.role)
    target_sub = _resolve_target(inp.target)
    # ctx.org_id = org parente injectée par GROUP_ADMIN_OF (jamais un param client).
    if org_store.get_org_role(ctx.org_id, target_sub) is None:
        raise AuthzDenied(409, "not_org_member",
                          "La cible doit d'abord être membre de l'org parente.")
    group_store.add_group_member(inp.group_id, target_sub, role)
    return {"ok": True, "group_id": inp.group_id, "sub": target_sub, "role": role}


def _set_member_role(ctx: ResolvedCtx, inp: SetGroupMemberRoleInput) -> dict:
    role = _check_role(inp.role)
    current = group_store.get_group_role(inp.group_id, inp.sub)
    if current is None:
        raise AuthzDenied(404, "not_a_member", "Cible non-membre du groupe.")
    if current == "group_admin" and role != "group_admin" \
            and group_store.count_group_admins(inp.group_id) <= 1:
        raise AuthzDenied(409, "last_group_admin",
                          "Impossible de rétrograder le dernier chef d'équipe.")
    group_store.add_group_member(inp.group_id, inp.sub, role)
    return {"ok": True, "group_id": inp.group_id, "sub": inp.sub, "role": role}


def _remove_member(ctx: ResolvedCtx, inp: RemoveGroupMemberInput) -> dict:
    target_sub = _resolve_target(inp.target)
    if group_store.get_group_role(inp.group_id, target_sub) == "group_admin" \
            and group_store.count_group_admins(inp.group_id) <= 1:
        raise AuthzDenied(409, "last_group_admin",
                          "Impossible de retirer le dernier chef d'équipe.")
    if not group_store.remove_group_member(inp.group_id, target_sub):
        raise AuthzDenied(404, "not_a_member", "Cible non-membre du groupe.")
    return {"ok": True, "group_id": inp.group_id, "sub": target_sub, "removed": True}


CAPABILITIES += [
    Capability(
        key="group.member.add", handler=_add_member, Input=AddGroupMemberInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description=("Add a member (by email or sub) to a group you lead. The target "
                     "must already belong to the parent org. role: group_member|group_admin."),
        rest=RestBinding("POST", "/api/groups/{id}/members", _GID),
    ),
    Capability(
        key="group.member.set_role", handler=_set_member_role, Input=SetGroupMemberRoleInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Change a member's role in a group you lead (group_member|group_admin).",
        rest=RestBinding("POST", "/api/groups/{id}/members/{sub}", _GID),
    ),
    Capability(
        key="group.member.remove", handler=_remove_member, Input=RemoveGroupMemberInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Remove a member (by email or sub) from a group you lead.",
        rest=RestBinding("DELETE", "/api/groups/{id}/members/{sub}",
                         {"id": "group_id", "sub": "target"}),
    ),
]
