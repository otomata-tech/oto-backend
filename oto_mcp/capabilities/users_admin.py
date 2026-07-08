"""Capacités d'administration centrées USER (ADR 0009).

Migre le bloc user-admin (jadis REST-only, écrit main dans `api_routes.py`) vers
des capacités co-déclarées → faces MCP **et** REST dérivées d'une seule déclaration,
sur les **mêmes chemins REST** (dashboard inchangé). Permet de setup complètement un
compte depuis Claude : retrouver un user, voir son état, poser son rôle, lui grant une
clé plateforme (user/org), offrir une option payante.

Logique reprise verbatim des handlers d'origine ; gates préservés à l'identique
(list/detail = PLATFORM_ADMIN, écritures = SUPER_ADMIN). Confort MCP : `_resolve_target`
accepte un email OU un sub (côté REST le `{sub}` du path mappe vers `target`).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import access, connectors, credentials_store, db, group_store, org_store
from ._authz import PLATFORM_ADMIN, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_SUB = {"sub": "target"}   # route {sub} (dashboard) → champ Input `target`
_ID = {"id": "org_id"}


def _resolve_target(target: str) -> str:
    """Email → sub (404 si inconnu), sinon sub brut. Miroir de
    `tools/meta._resolve_target_sub` pour le confort MCP (l'agent passe un email)."""
    if "@" in target:
        u = db.get_user_by_email(target)
        if not u:
            raise AuthzDenied(404, "unknown_user", f"Aucun compte avec l'email {target!r}.")
        return u["sub"]
    return target


# ── Input models (seule source de validation) ───────────────────────────────
class UserListInput(BaseModel):
    query: Optional[str] = None   # filtre email/name/sub (MCP) ; absent côté dashboard


class UserGetInput(BaseModel):
    target: str                   # email ou sub


class SetRoleInput(BaseModel):
    target: str
    role: str


# ADR 0044 §F R4 : le grant vise le CONNECTEUR (provider) au lieu d'un surrogate key_id
# (la table platform_keys disparaît). L'instance ciblée = la clé plateforme du provider.
class GrantKeyInput(BaseModel):
    target: str
    provider: str
    daily_quota: Optional[int] = None


class RevokeKeyInput(BaseModel):
    target: str
    provider: str


class OrgGrantKeyInput(BaseModel):
    org_id: int
    provider: str
    daily_quota: Optional[int] = None


class OrgRevokeKeyInput(BaseModel):
    org_id: int
    provider: str


class OptionInput(BaseModel):
    entity_type: Literal["user", "org"]
    entity_id: str
    option: str
    on: bool


# ── Handlers (core, (ctx, inp) -> dict) ──────────────────────────────────────
def _list_users(ctx: ResolvedCtx, inp: UserListInput) -> dict:
    users = db.list_users_with_grants()  # inclut les grants pour la matrice users × keys
    for u in users:
        u["effective_role"] = access.get_user_role(u["sub"])  # rôle effectif (OTO_MCP_ADMIN_SUB)
    if inp.query:
        q = inp.query.lower()
        users = [u for u in users
                 if q in (u.get("email") or "").lower()
                 or q in (u.get("name") or "").lower()
                 or q in u["sub"].lower()]
    return {"users": users}


def _user_detail(ctx: ResolvedCtx, inp: UserGetInput) -> dict:
    target = _resolve_target(inp.target)
    u = db.get_user(target)
    if not u:
        raise AuthzDenied(404, "unknown_user", f"Compte {target!r} inconnu.")
    # Contexte PERSISTÉ de la cible (org/équipe maison) — PAS current_org/current_group,
    # qui renverraient le contexte view-as/session du REQUÉRANT admin (fuite vécue
    # 2026-06-24 : la fiche montrait l'option de l'org du requérant, pas de la cible).
    target_org = org_store.get_active_org(target)
    target_group = group_store.get_active_group(target)
    status = access.status_for(target, org=target_org, group=target_group)
    orgs = org_store.list_orgs_for_user(target)
    # Messagerie Unipile PAR ORG (l'option est per-org ; un user peut être dans N orgs) :
    # un bloc par org, option/canaux calculés CONTRE cette org (jamais current_org).
    from ..tools import unipile
    unipile_orgs = unipile.admin_status_by_org(target, orgs)
    return {
        "sub": target, "email": u.get("email"), "name": u.get("name"),
        "role": status["role"], "active_org": status.get("active_org"),
        "orgs": orgs,
        "providers": status["providers"],
        "grants": db.list_grants_for_user(target),
        "option_comps": db.list_option_comps("user", target),  # couche 3 (comp user)
        "unipile_orgs": unipile_orgs,   # état messagerie par org (b)
    }


def _set_role(ctx: ResolvedCtx, inp: SetRoleInput) -> dict:
    if inp.role not in access.ROLES:
        raise AuthzDenied(400, "invalid_role", f"Rôle invalide. Valides : {list(access.ROLES)}.")
    target = _resolve_target(inp.target)
    if not db.get_user(target):
        raise AuthzDenied(404, "unknown_user", f"Compte {target!r} inconnu.")
    db.set_user_role(target, inp.role)
    return {"ok": True, "sub": target, "role": inp.role}


def _has_platform_instance(provider: str) -> bool:
    return bool(credentials_store.list_platform_instances(provider))


def _grant_key(ctx: ResolvedCtx, inp: GrantKeyInput) -> dict:
    target = _resolve_target(inp.target)
    if not db.get_user(target):
        raise AuthzDenied(404, "unknown_user", f"Compte {target!r} inconnu.")
    if not _has_platform_instance(inp.provider):
        raise AuthzDenied(404, "unknown_key", f"Aucune clé plateforme `{inp.provider}`.")
    dq = max(1, inp.daily_quota) if inp.daily_quota is not None else None
    credentials_store.platform_grant(inp.provider, f"user:{target}", daily_quota=dq)
    return {"ok": True, "sub": target, "provider": inp.provider, "daily_quota": dq}


def _revoke_key(ctx: ResolvedCtx, inp: RevokeKeyInput) -> dict:
    target = _resolve_target(inp.target)
    credentials_store.platform_revoke(inp.provider, f"user:{target}")
    return {"ok": True, "sub": target, "provider": inp.provider}


def _grant_org_key(ctx: ResolvedCtx, inp: OrgGrantKeyInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if not _has_platform_instance(inp.provider):
        raise AuthzDenied(404, "unknown_key", f"Aucune clé plateforme `{inp.provider}`.")
    dq = max(1, inp.daily_quota) if inp.daily_quota is not None else None
    credentials_store.platform_grant(inp.provider, f"org:{inp.org_id}", daily_quota=dq)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider, "daily_quota": dq}


def _revoke_org_key(ctx: ResolvedCtx, inp: OrgRevokeKeyInput) -> dict:
    credentials_store.platform_revoke(inp.provider, f"org:{inp.org_id}")
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider}


def _set_option(ctx: ResolvedCtx, inp: OptionInput) -> dict:
    eid = str(inp.entity_id)
    if inp.entity_type == "user" and not db.get_user(eid):
        raise AuthzDenied(404, "unknown_user", f"Compte {eid!r} inconnu.")
    if inp.entity_type == "org":
        try:
            if not org_store.get_org(int(eid)):
                raise AuthzDenied(404, "unknown_org", f"Org #{eid} inconnue.")
        except (ValueError, TypeError):
            raise AuthzDenied(400, "invalid_body", "entity_id d'org doit être un entier.")
    if inp.on:
        db.set_option_comp(inp.entity_type, eid, inp.option, granted_by=ctx.sub)
        key = _compose_platform_grant(ctx, inp, eid)
    else:
        db.clear_option_comp(inp.entity_type, eid, inp.option)
        key = _compose_platform_revoke(inp, eid)
    return {"ok": True, "entity_type": inp.entity_type, "entity_id": eid,
            "option": inp.option, "on": inp.on, "platform_key": key}


def _compose_platform_grant(ctx: ResolvedCtx, inp: OptionInput, eid: str) -> Optional[dict]:
    """Composition opérateur (ADR 0024) : « offrir l'option » ne doit pas laisser un
    **état mort**. Comper l'option (couche 3, `has_option`) sans donner la clé
    (couche 2) = `has_option`=true mais aucune clé à résoudre → 404 au `/connect`
    (bouton « Connecter » inerte). Pour un connecteur en mode **plateforme** (revente),
    on accorde donc AUSSI la clé plateforme — le grant = droit d'usage de la clé
    partagée. Les deux couches restent **séparées en base** (orthogonales, ADR 0024) ;
    c'est l'ACTION admin qui les compose. Renvoie un compte-rendu, jamais la clé.

    `None` = l'option n'est pas un connecteur en mode plateforme (rien à composer ;
    p.ex. une option non liée à un connecteur) → comp simple."""
    con = connectors.connector_for_provider(inp.option)
    if not con or "platform" not in con.auth_modes:
        return None
    if not credentials_store.list_platform_instances(inp.option):
        # Connecteur revente sans clé plateforme posée → la comp seule resterait un
        # état mort. On le signale au lieu de le masquer (cf. feedback governance UI).
        return {"granted": False, "reason": "no_platform_key",
                "hint": f"Aucune clé plateforme {inp.option!r} posée — pose-la au dashboard "
                        "(/platform/connectors) pour que l'option soit utilisable."}
    if inp.entity_type == "user":
        credentials_store.platform_grant(inp.option, f"user:{eid}")  # ADR 0044 §F R4
        # État d'un TIERS → son org maison, jamais current_org du requérant
        # (seam acteur-scopé ADR 0023 ; scope membre ADR 0033).
        byo = db.has_member_api_key(eid, org_store.get_active_org(eid), inp.option)
    else:
        credentials_store.platform_grant(inp.option, f"org:{eid}")
        byo = org_store.has_org_secret(int(eid), inp.option)
    out = {"granted": True, "provider": inp.option}
    if byo:
        # L'entité a sa propre clé (BYO) → en résolution sa clé prime sur la
        # plateforme, et le gate d'option est court-circuité : l'option ET le
        # grant sont **inertes**. On le dit plutôt que de faire croire à un effet.
        out["byo_inert"] = True
    return out


def _compose_platform_revoke(inp: OptionInput, eid: str) -> Optional[dict]:
    """Symétrique de `_compose_platform_grant` : retirer la comp d'un connecteur en
    mode plateforme retire aussi le(s) grant(s) de sa clé plateforme — « retirer
    l'option » = retirer l'accès revente d'un bloc (couche 2 + couche 3)."""
    con = connectors.connector_for_provider(inp.option)
    if not con or "platform" not in con.auth_modes:
        return None
    scope = f"user:{eid}" if inp.entity_type == "user" else f"org:{eid}"
    credentials_store.platform_revoke(inp.option, scope)  # ADR 0044 §F R4
    return {"revoked": True}


CAPABILITIES += [
    Capability(
        key="platform.user.list", handler=_list_users, Input=UserListInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] List all accounts (with their platform-key grants and "
                    "effective role). Optional `query` filters by email/name/sub substring.",
        rest=RestBinding("GET", "/api/admin/users"),
    ),
    Capability(
        key="platform.user.get", handler=_user_detail, Input=UserGetInput,
        authz=PLATFORM_ADMIN,
        description="[platform admin] Full account fiche by email or sub: identity, effective "
                    "per-provider access, platform-key grants, unlocked namespaces, paid-option comps.",
        rest=RestBinding("GET", "/api/admin/users/{sub}", _SUB),
    ),
    Capability(
        key="platform.user.set_role", handler=_set_role, Input=SetRoleInput,
        authz=SUPER_ADMIN,
        description="[super admin] Set an account's platform role (member|admin|super_admin). "
                    "target = email or sub.",
        rest=RestBinding("POST", "/api/admin/users/{sub}/role", _SUB),
    ),
    Capability(
        key="platform.key.grant", handler=_grant_key, Input=GrantKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Grant a platform key (by id) to a user, with an optional "
                    "per-day quota. target = email or sub. Never reveals the key.",
        rest=RestBinding("POST", "/api/admin/users/{sub}/grants/{provider}", _SUB),
    ),
    Capability(
        key="platform.key.revoke", handler=_revoke_key, Input=RevokeKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Revoke a user's grant of a connector's platform key.",
        rest=RestBinding("DELETE", "/api/admin/users/{sub}/grants/{provider}", _SUB),
    ),
    Capability(
        key="platform.org.grant_key", handler=_grant_org_key, Input=OrgGrantKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Share a connector's platform key with a WHOLE org — every member "
                    "resolves it (metered per-member). Optional per-day quota.",
        rest=RestBinding("POST", "/api/admin/orgs/{id}/grants/{provider}", _ID),
    ),
    Capability(
        key="platform.org.revoke_key", handler=_revoke_org_key, Input=OrgRevokeKeyInput,
        authz=SUPER_ADMIN,
        description="[super admin] Revoke an org's share of a connector's platform key.",
        rest=RestBinding("DELETE", "/api/admin/orgs/{id}/grants/{provider}", _ID),
    ),
    Capability(
        key="platform.option.set", handler=_set_option, Input=OptionInput,
        authz=SUPER_ADMIN,
        description="[super admin] Grant (on=true) or remove (on=false) a connector option as a FREE "
                    "comp for a user or org (e.g. option='unipile'). Read by access.has_option "
                    "(no billing — option governance is admin-only). entity_type='user'|'org', entity_id=sub|org_id. "
                    "For a platform-mode connector this ALSO grants/revokes its platform key (so "
                    "the option is actually usable, not a dead has_option without a key); the "
                    "`platform_key` field reports what happened (granted / no_platform_key / "
                    "byo_inert / revoked).",
        mcp="oto_admin_set_option",
        rest=RestBinding("POST", "/api/admin/option-comps", {}),
    ),
]
