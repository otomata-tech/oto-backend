"""Capacités d'invitation d'équipe (onboarding SaaS, ADR 0009).

create/list/revoke gatés `ORG_ADMIN_OF` (platform-admin par escalade) ; accept en
`SUB_ONLY` lié au secret du token ET à l'email vérifié du compte (anti-transfert
de lien). L'email part en best-effort (cf. `email.send_invite_email`) ; si non
envoyé, l'`invite_url` est renvoyé pour partage manuel.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from .. import db, email, org_store
from ._authz import ORG_ADMIN_OF, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}
_INVITE_TTL_DAYS = int(os.environ.get("OTO_MCP_INVITE_TTL_DAYS", "7"))


def _app_url() -> str:
    return os.environ.get("OTO_APP_URL", "https://dashboard.oto.ninja").rstrip("/")


class InviteCreateInput(BaseModel):
    org_id: int
    email: str
    role: str = "org_member"


class InviteListInput(BaseModel):
    org_id: int


class InviteRevokeInput(BaseModel):
    org_id: int
    invite_id: int


class InviteAcceptInput(BaseModel):
    token: str = Field(min_length=1)


def _invite_create(ctx: ResolvedCtx, inp: InviteCreateInput) -> dict:
    if inp.role not in org_store.ORG_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle invalide : {inp.role!r}.")
    if "@" not in (inp.email or ""):
        raise AuthzDenied(400, "invalid_email", "Email invalide.")
    org = org_store.get_org(inp.org_id)
    if not org:
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    _, token = org_store.create_invitation(
        inp.org_id, inp.email, inp.role, invited_by=ctx.sub, ttl_days=_INVITE_TTL_DAYS)
    invite_url = f"{_app_url()}/invite?token={token}"
    inviter = (db.get_user(ctx.sub) or {}).get("email")
    emailed = email.send_invite_email(inp.email.strip(), org["name"], invite_url, inviter)
    return {"ok": True, "email": inp.email.strip().lower(), "role": inp.role,
            "emailed": emailed, "invite_url": invite_url}


def _invite_list(ctx: ResolvedCtx, inp: InviteListInput) -> dict:
    return {"invitations": org_store.list_invitations(inp.org_id)}


def _invite_revoke(ctx: ResolvedCtx, inp: InviteRevokeInput) -> dict:
    if not org_store.revoke_invitation(inp.org_id, inp.invite_id):
        raise AuthzDenied(404, "unknown_invitation", "Invitation introuvable ou déjà acceptée.")
    return {"ok": True, "revoked": inp.invite_id}


def _invite_accept(ctx: ResolvedCtx, inp: InviteAcceptInput) -> dict:
    inv = org_store.get_invitation_by_token(inp.token)
    if not inv:
        raise AuthzDenied(410, "invalid_or_expired", "Invitation invalide, expirée ou déjà utilisée.")
    my_email = ((db.get_user(ctx.sub) or {}).get("email") or "").strip().lower()
    if my_email != inv["email"].strip().lower():
        raise AuthzDenied(403, "email_mismatch",
                          "Cette invitation vise une autre adresse email.")
    res = org_store.accept_invitation(inp.token, ctx.sub)
    if not res:
        raise AuthzDenied(410, "invalid_or_expired", "Invitation invalide ou déjà utilisée.")
    org = org_store.get_org(res["org_id"])
    return {"ok": True, "org_id": res["org_id"], "org_role": res["org_role"],
            "active_org": res["org_id"], "name": org["name"] if org else None}


CAPABILITIES += [
    Capability(
        key="org.invite.create", handler=_invite_create, Input=InviteCreateInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Invite someone by email to an org you administer (role: org_member|org_admin). Sends an email link.",
        mcp="oto_invite_member",
        rest=(RestBinding("POST", "/api/orgs/{id}/invitations", _ID),
              RestBinding("POST", "/api/admin/orgs/{id}/invitations", _ID)),
    ),
    Capability(
        key="org.invite.list", handler=_invite_list, Input=InviteListInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="List pending invitations for an org you administer.",
        rest=RestBinding("GET", "/api/orgs/{id}/invitations", _ID),
    ),
    Capability(
        key="org.invite.revoke", handler=_invite_revoke, Input=InviteRevokeInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Revoke a pending invitation.",
        rest=RestBinding("DELETE", "/api/orgs/{id}/invitations/{inv}",
                         {"id": "org_id", "inv": "invite_id"}),
    ),
    Capability(
        key="org.invite.accept", handler=_invite_accept, Input=InviteAcceptInput,
        authz=SUB_ONLY,
        description="Accept an org invitation with its token (must match your verified email).",
        mcp="oto_accept_invite",
        rest=RestBinding("POST", "/api/me/invitations/accept"),
    ),
]
