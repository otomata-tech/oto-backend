"""Capacités d'invitation d'ÉQUIPE (feature cascade plateforme/org/équipe, ADR 0012).

Même modèle bearer que l'invitation d'org (`orgs_invites`), au grain équipe : le chef
d'équipe invite quelqu'un DANS son équipe. À l'acceptation, l'invité rejoint l'org
parente PUIS l'équipe (invariant équipe ⊂ org) — un email pas encore inscrit peut donc
être invité directement dans une équipe, sans passer par l'org d'abord.

Autz = `GROUP_ADMIN_OF` (chef d'équipe, org_admin parent, ou platform_admin par
escalade `roles`). L'org parente est injectée par le combinateur (`ctx.org_id`), jamais
un param client. L'acceptation passe par la même capacité `org.invite.accept`.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import group_store, org_store
from . import orgs_invites
from ._authz import GROUP_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_GID = {"id": "group_id"}


class GroupInviteCreateInput(BaseModel):
    group_id: int
    email: str | None = None
    role: str = "group_member"          # group_member | group_admin
    send_email: bool = True


class GroupInviteListInput(BaseModel):
    group_id: int


class GroupInviteRevokeInput(BaseModel):
    group_id: int
    invite_id: int


def _invite_create(ctx: ResolvedCtx, inp: GroupInviteCreateInput) -> dict:
    if inp.role not in group_store.GROUP_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle d'équipe invalide : {inp.role!r}.")
    grp = group_store.get_group(inp.group_id)
    if not grp:
        raise AuthzDenied(404, "unknown_group", f"Équipe #{inp.group_id} inconnue.")
    # ctx.org_id = org parente injectée par GROUP_ADMIN_OF → l'invité rejoint cette org
    # (rôle membre) puis l'équipe avec le rôle demandé.
    return orgs_invites.emit_invitation(
        ctx, org_id=ctx.org_id, email=inp.email, send_email=inp.send_email,
        source="group_admin", role="org_member", target_name=grp["name"],
        group_id=inp.group_id, group_role=inp.role)


def _invite_list(ctx: ResolvedCtx, inp: GroupInviteListInput) -> dict:
    return {"invitations": org_store.list_group_invitations(inp.group_id)}


def _invite_revoke(ctx: ResolvedCtx, inp: GroupInviteRevokeInput) -> dict:
    if not org_store.revoke_group_invitation(inp.group_id, inp.invite_id):
        raise AuthzDenied(404, "unknown_invitation", "Invitation introuvable ou déjà acceptée.")
    return {"ok": True, "revoked": inp.invite_id}


CAPABILITIES += [
    Capability(
        key="group.invite.create", handler=_invite_create, Input=GroupInviteCreateInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description=("Invite someone to a team you lead (role: group_member|group_admin). "
                     "They join the parent org then the team on accept. send_email=true "
                     "mails a link; false returns a short code to share yourself."),
        rest=RestBinding("POST", "/api/groups/{id}/invitations", _GID),
    ),
    Capability(
        key="group.invite.list", handler=_invite_list, Input=GroupInviteListInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="List pending invitations for a team you lead.",
        rest=RestBinding("GET", "/api/groups/{id}/invitations", _GID),
    ),
    Capability(
        key="group.invite.revoke", handler=_invite_revoke, Input=GroupInviteRevokeInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Revoke a pending team invitation.",
        rest=RestBinding("DELETE", "/api/groups/{id}/invitations/{inv}",
                         {"id": "group_id", "inv": "invite_id"}),
    ),
]
