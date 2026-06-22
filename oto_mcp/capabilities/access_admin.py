"""Capacités d'administration de l'accès plateforme (ADR 0013).

Gate doux : les cold signups atterrissent en `pending` (waitlist) ; un platform
admin approuve (→ `active` + quota referral), rejette (→ `blocked`, sort de la
file ; réversible par un grant ultérieur) ou ajuste le quota. La file d'attente
est une vue dérivée (`db.list_waitlist`), jamais une table. `grant_access` envoie
l'email « accès ouvert » (best-effort, mailer otomata).
"""
from __future__ import annotations

import os

from pydantic import BaseModel

from .. import db, email, oauth_facade
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_SUB = {"sub": "sub"}


def _app_url() -> str:
    return os.environ.get("OTO_APP_URL", "https://dashboard.oto.ninja").rstrip("/")


def _default_quota() -> int:
    return int(os.environ.get("OTO_ALPHA_INVITE_QUOTA", "5"))


class WaitlistInput(BaseModel):
    pass


class GrantAccessInput(BaseModel):
    sub: str
    quota: int | None = None


class RejectAccessInput(BaseModel):
    sub: str


class SetQuotaInput(BaseModel):
    sub: str
    quota: int


def _list_waitlist(ctx: ResolvedCtx, inp: WaitlistInput) -> dict:
    rows = db.list_waitlist()
    return {"waitlist": rows, "count": len(rows)}


def _grant_access(ctx: ResolvedCtx, inp: GrantAccessInput) -> dict:
    user = db.get_user(inp.sub)
    if not user:
        raise AuthzDenied(404, "unknown_user", f"Compte {inp.sub!r} inconnu.")
    quota = _default_quota() if inp.quota is None else int(inp.quota)
    db.grant_platform_access(inp.sub, quota=quota)
    emailed = False
    if user.get("email"):
        # Magic-link : l'approbation logge l'user en un clic (pas de re-saisie de code).
        app_url = oauth_facade.magic_url(_app_url(), user["email"])
        emailed = email.send_access_granted_email(user["email"], app_url)
    return {"ok": True, "sub": inp.sub, "access_status": "active",
            "invite_quota": quota, "emailed": emailed}


def _reject_access(ctx: ResolvedCtx, inp: RejectAccessInput) -> dict:
    if not db.get_user(inp.sub):
        raise AuthzDenied(404, "unknown_user", f"Compte {inp.sub!r} inconnu.")
    db.block_platform_access(inp.sub)
    return {"ok": True, "sub": inp.sub, "access_status": "blocked"}


def _set_quota(ctx: ResolvedCtx, inp: SetQuotaInput) -> dict:
    if not db.get_user(inp.sub):
        raise AuthzDenied(404, "unknown_user", f"Compte {inp.sub!r} inconnu.")
    db.set_invite_quota(inp.sub, inp.quota)
    return {"ok": True, "sub": inp.sub, "invite_quota": int(inp.quota)}


CAPABILITIES += [
    Capability(
        key="platform.access.waitlist", handler=_list_waitlist, Input=WaitlistInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] List accounts awaiting alpha access (the waitlist).",
        mcp="oto_admin_list_waitlist",
        rest=RestBinding("GET", "/api/admin/waitlist"),
    ),
    Capability(
        key="platform.access.grant", handler=_grant_access, Input=GrantAccessInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] Grant alpha access to an account (sets active + invite quota, "
                    "emails them). quota defaults to OTO_ALPHA_INVITE_QUOTA.",
        mcp="oto_admin_grant_access",
        rest=RestBinding("POST", "/api/admin/users/{sub}/access", _SUB),
    ),
    Capability(
        key="platform.access.reject", handler=_reject_access, Input=RejectAccessInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] Reject an account's access request (sets blocked; "
                    "drops it from the waitlist). Reversible via grant.",
        mcp="oto_admin_reject_access",
        rest=RestBinding("POST", "/api/admin/users/{sub}/block", _SUB),
    ),
    Capability(
        key="platform.access.set_quota", handler=_set_quota, Input=SetQuotaInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] Set an account's remaining alpha invitation quota.",
        mcp="oto_admin_set_quota",
        rest=RestBinding("PUT", "/api/admin/users/{sub}/quota", _SUB),
    ),
]
