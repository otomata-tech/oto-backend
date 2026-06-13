"""Capacités d'écriture sur les membres d'org (ADR 0009, barreau 2).

Réconcilie la divergence d'autz : la MÊME opération était platform-admin-only
en MCP et org_admin self-service en REST. Unifiée sur **`ORG_ADMIN_OF`**
(org_admin de l'org, platform-admin par escalade) — décision utilisateur. Les
deux chemins REST historiques (self `/api/orgs/{id}/…` + admin
`/api/admin/orgs/{id}/…`) sont conservés via le multi-binding (même
handler+autz) pour ne casser aucune vue du dashboard.

Contrat MCP : `org_id` (entier) remplace l'ancien `org` (id-ou-nom).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import db, org_store
from ._authz import ORG_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}  # placeholder de route {id} → champ Input org_id


def _resolve_target(target: str) -> str:
    """Email (d'un user déjà connecté) ou sub direct → sub. Lève AuthzDenied neutre."""
    target = (target or "").strip()
    if not target:
        raise AuthzDenied(400, "missing_target", "Cible (email ou sub) requise.")
    if "@" in target:
        user = db.get_user_by_email(target)
        if not user:
            raise AuthzDenied(400, "unknown_user",
                              f"Aucun user connu avec l'email `{target}` (doit s'être connecté une fois).")
        return user["sub"]
    return target


def _count_org_admins(org_id: int) -> int:
    return sum(1 for m in org_store.list_org_members(org_id) if m["org_role"] == "org_admin")


def _check_role(role: str) -> str:
    if role not in org_store.ORG_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle invalide : {role!r}.")
    return role


def _require_org_exists(org_id: int) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")


class AddMemberInput(BaseModel):
    org_id: int
    target: str                       # email ou sub
    role: str = "org_member"


class SetMemberRoleInput(BaseModel):
    org_id: int
    sub: str                          # membre cible (sub)
    role: str


class RemoveMemberInput(BaseModel):
    org_id: int
    target: str                       # email ou sub (route {sub} → target)


def _add_member(ctx: ResolvedCtx, inp: AddMemberInput) -> dict:
    _require_org_exists(inp.org_id)
    role = _check_role(inp.role)
    target_sub = _resolve_target(inp.target)
    org_store.add_org_member(inp.org_id, target_sub, role)
    return {"ok": True, "org_id": inp.org_id, "sub": target_sub, "role": role}


def _set_member_role(ctx: ResolvedCtx, inp: SetMemberRoleInput) -> dict:
    _require_org_exists(inp.org_id)
    role = _check_role(inp.role)
    current = org_store.get_org_role(inp.org_id, inp.sub)
    if current is None:
        raise AuthzDenied(404, "not_a_member", "Cible non-membre de l'org.")
    # Anti-lockout : ne pas rétrograder le dernier org_admin.
    if current == "org_admin" and role != "org_admin" and _count_org_admins(inp.org_id) <= 1:
        raise AuthzDenied(409, "last_org_admin", "Impossible de rétrograder le dernier org_admin.")
    org_store.add_org_member(inp.org_id, inp.sub, role)
    return {"ok": True, "org_id": inp.org_id, "sub": inp.sub, "role": role}


def _remove_member(ctx: ResolvedCtx, inp: RemoveMemberInput) -> dict:
    target_sub = _resolve_target(inp.target)
    # Anti-lockout : ne pas retirer le dernier org_admin.
    if org_store.get_org_role(inp.org_id, target_sub) == "org_admin" and _count_org_admins(inp.org_id) <= 1:
        raise AuthzDenied(409, "last_org_admin", "Impossible de retirer le dernier org_admin.")
    if not org_store.remove_org_member(inp.org_id, target_sub):
        raise AuthzDenied(404, "not_a_member", "Cible non-membre de l'org.")
    return {"ok": True, "org_id": inp.org_id, "sub": target_sub, "removed": True}


CAPABILITIES += [
    Capability(
        key="org.member.add", handler=_add_member, Input=AddMemberInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Add a member (by email or sub) to an org you administer. role: org_member|org_admin.",
        mcp="oto_admin_add_org_member",
        rest=(RestBinding("POST", "/api/orgs/{id}/members", _ID),
              RestBinding("POST", "/api/admin/orgs/{id}/members", _ID)),
    ),
    Capability(
        key="org.member.set_role", handler=_set_member_role, Input=SetMemberRoleInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Change a member's role in an org you administer (org_member|org_admin).",
        rest=(RestBinding("POST", "/api/orgs/{id}/members/{sub}", _ID),
              RestBinding("POST", "/api/admin/orgs/{id}/members/{sub}", _ID)),
    ),
    Capability(
        key="org.member.remove", handler=_remove_member, Input=RemoveMemberInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Remove a member (by email or sub) from an org you administer.",
        mcp="oto_admin_remove_org_member",
        rest=(RestBinding("DELETE", "/api/orgs/{id}/members/{sub}", {"id": "org_id", "sub": "target"}),
              RestBinding("DELETE", "/api/admin/orgs/{id}/members/{sub}", {"id": "org_id", "sub": "target"})),
    ),
]
