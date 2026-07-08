"""Capacités d'invitation d'org (onboarding SaaS, ADR 0009).

Émission : une invitation a TOUJOURS un code court partageable (lien
`/invitation/<code>`) ET, si on le demande, part par mail. L'émetteur choisit
`send_email` ; sans envoi, il partage le code lui-même.

create/list/revoke gatés `ORG_ADMIN_OF` (platform-admin par escalade) ; accept en
`SUB_ONLY` (modèle bearer : le code/token suffit, cf. `org_store.accept_*`).
"""
from __future__ import annotations

import os

from pydantic import BaseModel

from .. import db, email, oauth_facade, org_store
from ._authz import ORG_ADMIN_OF, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}
_INVITE_TTL_DAYS = int(os.environ.get("OTO_MCP_INVITE_TTL_DAYS", "7"))


def _invite_base() -> str:
    """Base PUBLIQUE des liens d'invitation partagés (court, marketing).
    `oto.cx/invitation/...` redirige vers le dashboard (règle Caddy)."""
    return os.environ.get("OTO_INVITE_BASE_URL", "https://oto.cx").rstrip("/")


def _nominal_url(code: str, email_addr: str | None = None) -> str:
    """Lien d'une invitation nominative : `/invitation/<code>`. Augmenté d'un
    magic-link Logto (OTT) quand on connaît l'email invité → connexion sans saisie
    de code. Sans email = lien nu, partageable à la main."""
    url = f"{_invite_base()}/invitation/{code}"
    return oauth_facade.magic_url(url, email_addr.strip()) if email_addr else url


def _norm_email(raw: str | None, *, required: bool) -> str | None:
    e = (raw or "").strip().lower() or None
    if required and not e:
        raise AuthzDenied(400, "invalid_email", "Email requis pour un envoi par mail.")
    if e is not None and "@" not in e:
        raise AuthzDenied(400, "invalid_email", "Email invalide.")
    return e


# --- Inputs -----------------------------------------------------------------

class InviteCreateInput(BaseModel):
    org_id: int
    email: str | None = None
    role: str = "org_member"
    send_email: bool = True


class InviteListInput(BaseModel):
    org_id: int


class InviteRevokeInput(BaseModel):
    org_id: int
    invite_id: int


class InviteAcceptInput(BaseModel):
    token: str | None = None
    code: str | None = None


# --- Handlers ---------------------------------------------------------------

def _invite_create(ctx: ResolvedCtx, inp: InviteCreateInput) -> dict:
    if inp.role not in org_store.ORG_ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle invalide : {inp.role!r}.")
    email_addr = _norm_email(inp.email, required=inp.send_email)
    org = org_store.get_org(inp.org_id)
    if not org:
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    _, _token, code = org_store.create_invitation(
        inp.org_id, email_addr, inp.role, invited_by=ctx.sub,
        ttl_days=_INVITE_TTL_DAYS, source="org_admin")
    share_url = _nominal_url(code)
    emailed = False
    if inp.send_email and email_addr:
        inviter = (db.get_user(ctx.sub) or {}).get("email")
        emailed = email.send_invite_email(
            email_addr, org["name"], _nominal_url(code, email_addr), inviter)
    return {"ok": True, "email": email_addr, "role": inp.role, "code": code,
            "invite_url": share_url, "emailed": emailed}


def _invite_list(ctx: ResolvedCtx, inp: InviteListInput) -> dict:
    return {"invitations": org_store.list_invitations(inp.org_id)}


def _invite_revoke(ctx: ResolvedCtx, inp: InviteRevokeInput) -> dict:
    if not org_store.revoke_invitation(inp.org_id, inp.invite_id):
        raise AuthzDenied(404, "unknown_invitation", "Invitation introuvable ou déjà acceptée.")
    return {"ok": True, "revoked": inp.invite_id}


def _invite_accept(ctx: ResolvedCtx, inp: InviteAcceptInput) -> dict:
    """Accepte une invitation d'org par token mail (legacy) ou code court nominatif.
    Modèle bearer : le secret suffit."""
    if inp.token:
        res = org_store.accept_invitation(inp.token, ctx.sub)
    elif inp.code:
        res = org_store.accept_invitation_by_code(inp.code, ctx.sub)
    else:
        raise AuthzDenied(400, "missing_token", "Aucun token ni code d'invitation fourni.")
    if not res:
        raise AuthzDenied(410, "invalid_or_expired", "Invitation invalide, expirée ou déjà utilisée.")
    org = org_store.get_org(res["org_id"])
    return {"ok": True, "org_id": res["org_id"], "org_role": res["org_role"],
            "active_org": res["org_id"], "name": org["name"] if org else None}


CAPABILITIES += [
    Capability(
        key="org.invite.create", handler=_invite_create, Input=InviteCreateInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Invite someone to an org you administer (role: org_member|org_admin). "
                    "send_email=true mails a link; false returns a short code to share yourself.",
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
        description="Accept an org invitation by mail token or short code. Joins the org.",
        rest=RestBinding("POST", "/api/me/invitations/accept"),
    ),
]
