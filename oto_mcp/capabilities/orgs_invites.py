"""Capacités d'invitation (onboarding SaaS + accès alpha, ADR 0009/0013).

Émission unifiée (refonte 2026-06-22) : une invitation a TOUJOURS un code court
partageable (lien `/invitation/<carrier>/<code>`) ET, si on le demande, part par
mail. L'émetteur choisit `send_email` ; sans envoi, il partage le code lui-même.
Le **lien referral réutilisable** (`/invitation/<carrier>`) se diffuse au réseau et
puise dans le MÊME budget que les invitations directes (décision 2026-06-22). Le
budget est débité **à l'acceptation** (pas à l'émission) — cf. `org_store`.

create/list/revoke gatés `ORG_ADMIN_OF` (platform-admin par escalade) ; accept en
`SUB_ONLY` (modèle bearer : le code/token suffit, cf. `org_store.accept_*`).
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from .. import db, email, oauth_facade, org_store
from ._authz import ORG_ADMIN_OF, PLATFORM_ADMIN, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}
_INVITE_TTL_DAYS = int(os.environ.get("OTO_MCP_INVITE_TTL_DAYS", "7"))


def _app_url() -> str:
    """Base du dashboard (login, redirections post-accept)."""
    return os.environ.get("OTO_APP_URL", "https://dashboard.oto.ninja").rstrip("/")


def _invite_base() -> str:
    """Base PUBLIQUE des liens d'invitation partagés (court, marketing).
    `oto.ninja/invitation/...` redirige vers le dashboard (règle Caddy)."""
    return os.environ.get("OTO_INVITE_BASE_URL", "https://oto.ninja").rstrip("/")


def _nominal_url(carrier: str, code: str, email_addr: str | None = None) -> str:
    """Lien d'une invitation nominative : `/invitation/<carrier>/<code>`. Augmenté
    d'un magic-link Logto (OTT) quand on connaît l'email invité → connexion sans
    saisie de code. Sans email = lien nu, partageable à la main."""
    url = f"{_invite_base()}/invitation/{carrier}/{code}"
    return oauth_facade.magic_url(url, email_addr.strip()) if email_addr else url


def _referral_url(carrier: str) -> str:
    """Lien referral réutilisable, à diffuser : `/invitation/<carrier>`."""
    return f"{_invite_base()}/invitation/{carrier}"


def _carrier_for(sub: str) -> str:
    """Code referral stable de l'émetteur (sert de préfixe `carrier` des liens)."""
    return org_store.get_or_create_referral_code(sub) or ""


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
    carrier: str | None = None


class ReferralLinkInput(BaseModel):
    pass


class AlphaInviteInput(BaseModel):
    email: str | None = None
    send_email: bool = True


class AlphaInviteAdminInput(BaseModel):
    email: str | None = None
    send_email: bool = True


class AlphaInviteListInput(BaseModel):
    pass


class AlphaInviteRevokeInput(BaseModel):
    id: int


class AlphaInviteResendInput(BaseModel):
    email: str = Field(min_length=1)


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
    carrier = _carrier_for(ctx.sub)
    share_url = _nominal_url(carrier, code)
    emailed = False
    if inp.send_email and email_addr:
        inviter = (db.get_user(ctx.sub) or {}).get("email")
        emailed = email.send_invite_email(
            email_addr, org["name"], _nominal_url(carrier, code, email_addr), inviter)
    return {"ok": True, "email": email_addr, "role": inp.role, "code": code,
            "invite_url": share_url, "emailed": emailed}


def _invite_list(ctx: ResolvedCtx, inp: InviteListInput) -> dict:
    return {"invitations": org_store.list_invitations(inp.org_id)}


def _invite_revoke(ctx: ResolvedCtx, inp: InviteRevokeInput) -> dict:
    if not org_store.revoke_invitation(inp.org_id, inp.invite_id):
        raise AuthzDenied(404, "unknown_invitation", "Invitation introuvable ou déjà acceptée.")
    return {"ok": True, "revoked": inp.invite_id}


def _referral_link(ctx: ResolvedCtx, inp: ReferralLinkInput) -> dict:
    """Lien referral réutilisable de l'utilisateur (à diffuser à son réseau) +
    budget restant. Le lien fonctionne tant que le compte est actif et a du budget
    (débité à chaque entrée parrainée)."""
    me = db.get_user(ctx.sub) or {}
    carrier = _carrier_for(ctx.sub)
    return {"referral_code": carrier, "url": _referral_url(carrier),
            "invites_left": int(me.get("invite_quota") or 0),
            "active": me.get("access_status") == "active"}


def _alpha_invite_create(ctx: ResolvedCtx, inp: AlphaInviteInput) -> dict:
    """Invitation directe (ADR 0013) : un alpha-user actif invite quelqu'un. Vérifie
    qu'il a du budget (débité à l'acceptation, pas ici). L'invité crée sa propre org."""
    me = db.get_user(ctx.sub) or {}
    if me.get("access_status") != "active":
        raise AuthzDenied(403, "not_active",
                          "Ton accès alpha n'est pas actif — tu ne peux pas encore inviter.")
    if int(me.get("invite_quota") or 0) <= 0:
        raise AuthzDenied(403, "no_quota", "Tu n'as plus d'invitations disponibles.")
    email_addr = _norm_email(inp.email, required=inp.send_email)
    _, _token, code = org_store.create_invitation(
        None, email_addr, "org_member", invited_by=ctx.sub,
        ttl_days=_INVITE_TTL_DAYS, source="user_quota")
    carrier = _carrier_for(ctx.sub)
    share_url = _nominal_url(carrier, code)
    emailed = False
    if inp.send_email and email_addr:
        emailed = email.send_alpha_invite_email(
            email_addr, _nominal_url(carrier, code, email_addr), me.get("name") or me.get("email"))
    return {"ok": True, "email": email_addr, "code": code, "invite_url": share_url,
            "emailed": emailed, "invites_left": int(me.get("invite_quota") or 0)}


def _alpha_invite_admin_create(ctx: ResolvedCtx, inp: AlphaInviteAdminInput) -> dict:
    """Invitation alpha émise par un platform admin (ADR 0013) : ouvre l'accès à un
    tiers SANS budget (source 'admin' → pas de débit à l'acceptation). L'invité crée
    sa propre org."""
    email_addr = _norm_email(inp.email, required=inp.send_email)
    _, _token, code = org_store.create_invitation(
        None, email_addr, "org_member", invited_by=ctx.sub,
        ttl_days=_INVITE_TTL_DAYS, source="admin")
    carrier = _carrier_for(ctx.sub)
    share_url = _nominal_url(carrier, code)
    emailed = False
    if inp.send_email and email_addr:
        emailed = email.send_alpha_invite_email(email_addr, _nominal_url(carrier, code, email_addr))
    return {"ok": True, "email": email_addr, "code": code, "invite_url": share_url,
            "emailed": emailed}


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
    Émission admin côté droits (hors budget)."""
    email_addr = _norm_email(inp.email, required=True)
    org_store.revoke_alpha_invitations_for_email(email_addr)
    _, _token, code = org_store.create_invitation(
        None, email_addr, "org_member", invited_by=ctx.sub,
        ttl_days=_INVITE_TTL_DAYS, source="admin")
    carrier = _carrier_for(ctx.sub)
    emailed = email.send_alpha_invite_email(email_addr, _nominal_url(carrier, code, email_addr))
    return {"ok": True, "email": email_addr, "code": code,
            "invite_url": _nominal_url(carrier, code), "emailed": emailed}


def _invite_accept(ctx: ResolvedCtx, inp: InviteAcceptInput) -> dict:
    """Accepte une invitation par token mail (legacy), code court nominatif, ou code
    porteur (lien referral réutilisable). Modèle bearer : le secret suffit."""
    try:
        if inp.token:
            res = org_store.accept_invitation(inp.token, ctx.sub)
        elif inp.code:
            res = org_store.accept_invitation_by_code(inp.code, ctx.sub)
        elif inp.carrier:
            res = org_store.accept_referral(inp.carrier, ctx.sub)
        else:
            raise AuthzDenied(400, "missing_token", "Aucun token, code ou lien referral fourni.")
    except org_store.InviteQuotaExhausted:
        raise AuthzDenied(409, "inviter_no_quota",
                          "Le parrain n'a plus d'invitations disponibles.")
    if not res:
        raise AuthzDenied(410, "invalid_or_expired", "Invitation invalide, expirée ou déjà utilisée.")
    if res.get("referral"):
        return {"ok": True, "referral": True, "org_id": None, "org_role": None,
                "active_org": None, "name": None, "self": res.get("self", False)}
    org = org_store.get_org(res["org_id"])
    return {"ok": True, "referral": False, "org_id": res["org_id"], "org_role": res["org_role"],
            "active_org": res["org_id"], "name": org["name"] if org else None}


CAPABILITIES += [
    Capability(
        key="org.invite.create", handler=_invite_create, Input=InviteCreateInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Invite someone to an org you administer (role: org_member|org_admin). "
                    "send_email=true mails a link; false returns a short code to share yourself.",
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
        description="Accept an invitation by mail token, short code, or referral carrier code. "
                    "Org invite → joins the org; alpha/referral → grants access, create your own org next.",
        mcp="oto_accept_invite",
        rest=RestBinding("POST", "/api/me/invitations/accept"),
    ),
    Capability(
        key="platform.referral.link", handler=_referral_link, Input=ReferralLinkInput,
        authz=SUB_ONLY,
        description="Get your reusable referral link to share with your network (and how many "
                    "invitations you have left). Each person who joins via it uses one.",
        mcp="oto_referral_link",
        rest=RestBinding("GET", "/api/me/referral-link"),
    ),
    Capability(
        key="platform.invite.alpha", handler=_alpha_invite_create, Input=AlphaInviteInput,
        authz=SUB_ONLY,
        description="Invite someone to Oto. send_email=true mails them; false returns a short code "
                    "to share yourself. They get their own account/org. Uses one of your invitations "
                    "when they join. Requires active access and remaining quota.",
        mcp="oto_invite_to_alpha",
        rest=RestBinding("POST", "/api/me/alpha-invites"),
    ),
    Capability(
        key="platform.invite.alpha_admin", handler=_alpha_invite_admin_create,
        Input=AlphaInviteAdminInput, authz=PLATFORM_ADMIN,
        description="[platform admin] Invite someone to the Oto alpha, without spending referral "
                    "quota. send_email toggles delivery (else returns a code). Their own account/org.",
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
