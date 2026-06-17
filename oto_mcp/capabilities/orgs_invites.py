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
from ._authz import ORG_ADMIN_OF, PLATFORM_ADMIN, SUB_ONLY
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


class AlphaInviteInput(BaseModel):
    email: str


class AlphaInviteAdminInput(BaseModel):
    email: str


class AlphaInviteListInput(BaseModel):
    pass


class AlphaInviteRevokeInput(BaseModel):
    id: int


class AlphaInviteResendInput(BaseModel):
    email: str


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


def _alpha_invite_create(ctx: ResolvedCtx, inp: AlphaInviteInput) -> dict:
    """Invitation virale (ADR 0013) : un alpha-user actif dépense une de ses
    invitations pour faire entrer quelqu'un. L'invité crée sa propre org."""
    if "@" not in (inp.email or ""):
        raise AuthzDenied(400, "invalid_email", "Email invalide.")
    me = db.get_user(ctx.sub) or {}
    if me.get("access_status") != "active":
        raise AuthzDenied(403, "not_active",
                          "Ton accès alpha n'est pas actif — tu ne peux pas encore inviter.")
    if not db.consume_invite_quota(ctx.sub):
        raise AuthzDenied(403, "no_quota", "Tu n'as plus d'invitations alpha disponibles.")
    try:
        _, token = org_store.create_invitation(
            None, inp.email, "org_member", invited_by=ctx.sub,
            ttl_days=_INVITE_TTL_DAYS, source="user_quota")
    except Exception:
        db.refund_invite_quota(ctx.sub)
        raise
    invite_url = f"{_app_url()}/invite?token={token}"
    inviter = me.get("name") or me.get("email")
    emailed = email.send_alpha_invite_email(inp.email.strip(), invite_url, inviter)
    remaining = (db.get_user(ctx.sub) or {}).get("invite_quota", 0)
    return {"ok": True, "email": inp.email.strip().lower(), "emailed": emailed,
            "invite_url": invite_url, "invites_left": remaining}


def _alpha_invite_admin_create(ctx: ResolvedCtx, inp: AlphaInviteAdminInput) -> dict:
    """Invitation alpha émise par un platform admin (ADR 0013) : ouvre l'accès au
    service à un email tiers **sans entamer de quota referral** et sans exiger que
    l'admin soit lui-même actif. C'est un referral (`org_id=None`) → à l'acceptation
    l'invité reçoit l'accès + son propre quota et crée sa propre org."""
    if "@" not in (inp.email or ""):
        raise AuthzDenied(400, "invalid_email", "Email invalide.")
    _, token = org_store.create_invitation(
        None, inp.email, "org_member", invited_by=ctx.sub,
        ttl_days=_INVITE_TTL_DAYS, source="admin")
    invite_url = f"{_app_url()}/invite?token={token}"
    emailed = email.send_alpha_invite_email(inp.email.strip(), invite_url)
    return {"ok": True, "email": inp.email.strip().lower(), "emailed": emailed,
            "invite_url": invite_url}


def _alpha_invite_list(ctx: ResolvedCtx, inp: AlphaInviteListInput) -> dict:
    """Invitations alpha en attente (referral, pas encore acceptées)."""
    return {"invitations": org_store.list_alpha_invitations()}


def _alpha_invite_revoke(ctx: ResolvedCtx, inp: AlphaInviteRevokeInput) -> dict:
    if not org_store.revoke_alpha_invitation(inp.id):
        raise AuthzDenied(404, "unknown_invitation", "Invitation introuvable ou déjà acceptée.")
    return {"ok": True, "revoked": inp.id}


def _alpha_invite_resend(ctx: ResolvedCtx, inp: AlphaInviteResendInput) -> dict:
    """Renvoie une invitation alpha : supersede les invitations en attente pour cet
    email (pour ne pas multiplier les liens valides), en émet une fraîche et la mail.
    Identique à une émission admin côté droits (hors quota)."""
    if "@" not in (inp.email or ""):
        raise AuthzDenied(400, "invalid_email", "Email invalide.")
    org_store.revoke_alpha_invitations_for_email(inp.email)
    _, token = org_store.create_invitation(
        None, inp.email, "org_member", invited_by=ctx.sub,
        ttl_days=_INVITE_TTL_DAYS, source="admin")
    invite_url = f"{_app_url()}/invite?token={token}"
    emailed = email.send_alpha_invite_email(inp.email.strip(), invite_url)
    return {"ok": True, "email": inp.email.strip().lower(), "emailed": emailed,
            "invite_url": invite_url}


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
    if res.get("referral"):
        # Accès alpha accordé ; l'invité crée ensuite sa propre org (org.create).
        return {"ok": True, "referral": True, "org_id": None, "org_role": None,
                "active_org": None, "name": None}
    org = org_store.get_org(res["org_id"])
    return {"ok": True, "referral": False, "org_id": res["org_id"], "org_role": res["org_role"],
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
        description="Accept an invitation with its token (must match your verified email). "
                    "Org invite → joins the org; alpha referral → grants access, create your own org next.",
        mcp="oto_accept_invite",
        rest=RestBinding("POST", "/api/me/invitations/accept"),
    ),
    Capability(
        key="platform.invite.alpha", handler=_alpha_invite_create, Input=AlphaInviteInput,
        authz=SUB_ONLY,
        description="Spend one of your alpha invitations to invite someone to Oto by email. "
                    "They get their own account/org. Requires active access and remaining quota.",
        rest=RestBinding("POST", "/api/me/alpha-invites"),
    ),
    Capability(
        key="platform.invite.alpha_admin", handler=_alpha_invite_admin_create,
        Input=AlphaInviteAdminInput, authz=PLATFORM_ADMIN,
        description="[platform admin] Invite someone to the Oto alpha by email, without spending "
                    "your own referral quota. They get their own account/org.",
        rest=RestBinding("POST", "/api/admin/alpha-invites"),
    ),
    Capability(
        key="platform.invite.alpha_list", handler=_alpha_invite_list, Input=AlphaInviteListInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] List pending alpha invitations (sent, not yet accepted).",
        rest=RestBinding("GET", "/api/admin/alpha-invites"),
    ),
    Capability(
        key="platform.invite.alpha_revoke", handler=_alpha_invite_revoke, Input=AlphaInviteRevokeInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] Revoke a pending alpha invitation by id.",
        rest=RestBinding("DELETE", "/api/admin/alpha-invites/{id}"),
    ),
    Capability(
        key="platform.invite.alpha_resend", handler=_alpha_invite_resend, Input=AlphaInviteResendInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] Resend an alpha invitation by email (supersedes pending links, "
                    "issues a fresh one, emails it). No referral quota spent.",
        rest=RestBinding("POST", "/api/admin/alpha-invites/resend"),
    ),
]
