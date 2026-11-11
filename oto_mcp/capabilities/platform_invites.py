"""Capacités d'invitation PLATEFORME (feature cascade plateforme/org/équipe).

Le sommet de la cascade : l'admin plateforme invite un nouvel utilisateur sur oto.
L'inscription est libre (ADR 0013 supersédé) → une invite plateforme n'est plus un
droit d'accès mais un geste d'onboarding + attribution. Org cible **optionnelle** :
- sans `org_id` → onboarding pur (l'invité aura son compte + org perso au signup) ;
- avec `org_id` → rattachement direct à une org choisie, avec son rôle d'org.

Autz = `PLATFORM_INVITE` (parité avec la console admin) : un admin plateforme pour
l'onboarding pur, le super admin seul dès que l'invitation porte une org. L'acceptation
passe par la même capacité `org.invite.accept`, qui tient la même règle. Faces REST par-verbe (idiomatique + dashboard) ; la
face MCP consolidée `oto_admin_invite` vit dans `admin_console`.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import org_store
from .orgs import invites as orgs_invites
from ._authz import PLATFORM_ADMIN, PLATFORM_INVITE
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class PlatformInviteCreateInput(BaseModel):
    email: str | None = None
    org_id: int | None = None           # None = onboarding pur ; sinon rattachement
    role: str = "org_member"            # utilisé seulement si org_id est fourni
    send_email: bool = True


class PlatformInviteRevokeInput(BaseModel):
    invite_id: int


class _NoInput(BaseModel):
    pass


def _invite_create(ctx: ResolvedCtx, inp: PlatformInviteCreateInput) -> dict:
    target_name = None
    org_id = inp.org_id
    if org_id is not None:
        if inp.role not in org_store.ORG_ROLES:
            raise AuthzDenied(400, "invalid_role", f"Rôle invalide : {inp.role!r}.")
        org = org_store.get_org(org_id)
        if not org:
            raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
        target_name = org["name"]
    return orgs_invites.emit_invitation(
        ctx, org_id=org_id, email=inp.email, send_email=inp.send_email,
        source="platform_admin", role=inp.role, target_name=target_name)


def _invite_list(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return {"invitations": org_store.list_platform_invitations()}


def _invite_revoke(ctx: ResolvedCtx, inp: PlatformInviteRevokeInput) -> dict:
    if not org_store.revoke_platform_invitation(inp.invite_id):
        raise AuthzDenied(404, "unknown_invitation", "Invitation introuvable ou déjà acceptée.")
    return {"ok": True, "revoked": inp.invite_id}


CAPABILITIES += [
    Capability(
        key="platform.invite.create", handler=_invite_create, Input=PlatformInviteCreateInput,
        authz=PLATFORM_INVITE,
        description=("[ADMIN] Invite a user to the oto platform. Optional `org_id` attaches "
                     "them directly to a chosen org (role org_member|org_admin) and is "
                     "reserved to the super admin; omit it for pure onboarding. "
                     "send_email=false returns the share link only."),
        rest=RestBinding("POST", "/api/admin/invitations"),
    ),
    Capability(
        key="platform.invite.list", handler=_invite_list, Input=_NoInput,
        authz=PLATFORM_ADMIN,
        description="[ADMIN] List pending platform invitations.",
        rest=RestBinding("GET", "/api/admin/invitations"),
    ),
    Capability(
        key="platform.invite.revoke", handler=_invite_revoke, Input=PlatformInviteRevokeInput,
        authz=PLATFORM_ADMIN,
        description="[ADMIN] Revoke a pending platform invitation.",
        rest=RestBinding("DELETE", "/api/admin/invitations/{inv}", {"inv": "invite_id"}),
    ),
]
