"""Console admin MCP consolidée par concept (ADR 0009, fusion `*_op`).

Réunit les verbes d'un même objet métier en UN outil `oto_admin_<objet>(op=…)`, au
service du provisioning d'org de bout en bout depuis Claude. L'autz reste DÉCLARÉE
(combinateur op-aware `ADMIN_BY_OP` quand les paliers divergent) — jamais redescendue
dans le handler. Les handlers de domaine sont **réutilisés tels quels** (on construit
leur Input spécifique depuis l'Input consolidé) ; aucune logique n'est dupliquée. Les
faces REST historiques ne bougent pas : on retire seulement le binding `mcp=` des
capacités d'origine.

Concepts : `oto_admin_org`, `oto_admin_org_member`, `oto_admin_user`,
`oto_admin_access`, `oto_admin_key_grant`. Hors périmètre (décision 2026-06-25) :
pose de secret brut (`set_org_secret`/`set_platform_key`) = dashboard-only.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db
from . import access_admin, orgs_admin, orgs_members, orgs_reads, users_admin
from ._authz import ADMIN_BY_OP, ORG_ADMIN_OF, PLATFORM_ADMIN, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


# ── oto_admin_org : create / archive / list / get ────────────────────────────
class OrgAdminInput(BaseModel):
    op: Literal["create", "archive", "list", "get"]
    name: Optional[str] = None        # create
    org_id: Optional[int] = None      # archive / get


def _org(ctx: ResolvedCtx, inp: OrgAdminInput) -> dict:
    if inp.op == "create":
        return orgs_admin._create_org(ctx, orgs_admin.CreateOrgInput(
            name=_need(inp.name, "missing_name", "`name` requis pour create.")))
    if inp.op == "list":
        return orgs_reads._list_all_orgs(ctx, orgs_reads.NoInput())
    oid = _need(inp.org_id, "missing_org", f"`org_id` requis pour {inp.op}.")
    if inp.op == "archive":
        return orgs_admin._archive_org(ctx, orgs_admin.OrgIdInput(org_id=oid))
    return orgs_reads._org_detail(ctx, orgs_reads.OrgIdInput(org_id=oid))  # get


# ── oto_admin_org_member : add / remove / set_role / list ────────────────────
class OrgMemberAdminInput(BaseModel):
    op: Literal["add", "remove", "set_role", "list"]
    org_id: int
    target: Optional[str] = None      # add/remove/set_role : email ou sub
    role: Optional[str] = None        # add/set_role


def _org_member(ctx: ResolvedCtx, inp: OrgMemberAdminInput) -> dict:
    if inp.op == "list":
        return {"org_id": inp.org_id, "members": orgs_reads._members(inp.org_id)}
    target = _need(inp.target, "missing_target", "`target` (email ou sub) requis.")
    if inp.op == "add":
        return orgs_members._add_member(ctx, orgs_members.AddMemberInput(
            org_id=inp.org_id, target=target, role=inp.role or "org_member"))
    if inp.op == "remove":
        return orgs_members._remove_member(ctx, orgs_members.RemoveMemberInput(
            org_id=inp.org_id, target=target))
    # set_role — résout target→sub (le handler de domaine prend un sub direct).
    role = _need(inp.role, "missing_role", "`role` requis pour set_role.")
    sub = orgs_members._resolve_target(target)
    return orgs_members._set_member_role(ctx, orgs_members.SetMemberRoleInput(
        org_id=inp.org_id, sub=sub, role=role))


# ── oto_admin_user : list / get / set_role ───────────────────────────────────
class UserAdminInput(BaseModel):
    op: Literal["list", "get", "set_role"]
    target: Optional[str] = None      # get/set_role : email ou sub
    query: Optional[str] = None       # list : filtre substring
    role: Optional[str] = None        # set_role : member|admin|super_admin


def _user(ctx: ResolvedCtx, inp: UserAdminInput) -> dict:
    if inp.op == "list":
        return users_admin._list_users(ctx, users_admin.UserListInput(query=inp.query))
    target = _need(inp.target, "missing_target", "`target` (email ou sub) requis.")
    if inp.op == "get":
        return users_admin._user_detail(ctx, users_admin.UserGetInput(target=target))
    role = _need(inp.role, "missing_role", "`role` requis pour set_role.")
    return users_admin._set_role(ctx, users_admin.SetRoleInput(target=target, role=role))


# ── oto_admin_access : waitlist / grant / reject (gate alpha, ADR 0013) ──────
class AccessAdminInput(BaseModel):
    op: Literal["waitlist", "grant", "reject"]
    sub: Optional[str] = None         # grant/reject
    quota: Optional[int] = None       # grant (optionnel)


def _access(ctx: ResolvedCtx, inp: AccessAdminInput) -> dict:
    if inp.op == "waitlist":
        return access_admin._list_waitlist(ctx, access_admin.WaitlistInput())
    sub = _need(inp.sub, "missing_sub", f"`sub` requis pour {inp.op}.")
    if inp.op == "grant":
        return access_admin._grant_access(ctx, access_admin.GrantAccessInput(sub=sub, quota=inp.quota))
    return access_admin._reject_access(ctx, access_admin.RejectAccessInput(sub=sub))


# ── oto_admin_key_grant : list / grant / revoke · scope user|org (DROITS, pas de secret) ─
class KeyGrantInput(BaseModel):
    op: Literal["list", "grant", "revoke"]
    scope: Optional[Literal["user", "org"]] = None  # grant/revoke seulement
    target: Optional[str] = None      # scope=user : email ou sub
    org_id: Optional[int] = None      # scope=org
    key_id: Optional[int] = None
    provider: Optional[str] = None    # op=list : filtre optionnel
    daily_quota: Optional[int] = None  # grant (optionnel)


def _key_grant(ctx: ResolvedCtx, inp: KeyGrantInput) -> dict:
    if inp.op == "list":
        # Inventaire des clés plateforme posées (quels vendors oto contracte). Le
        # SECRET n'est JAMAIS renvoyé — on ne montre que l'identité de la clé.
        keys = [
            {"key_id": k["id"], "provider": k["provider"], "label": k.get("label"),
             "created_at": k.get("created_at")}
            for k in db.list_platform_keys(inp.provider)
        ]
        return {"keys": keys, "count": len(keys)}
    scope = _need(inp.scope, "missing_scope", "`scope` (user|org) requis pour grant/revoke.")
    key_id = _need(inp.key_id, "missing_key", "`key_id` (clé plateforme) requis.")
    if inp.scope == "user":
        target = _need(inp.target, "missing_target", "scope=user : `target` requis.")
        if inp.op == "grant":
            return users_admin._grant_key(ctx, users_admin.GrantKeyInput(
                target=target, key_id=key_id, daily_quota=inp.daily_quota))
        return users_admin._revoke_key(ctx, users_admin.RevokeKeyInput(target=target, key_id=key_id))
    org_id = _need(inp.org_id, "missing_org", "scope=org : `org_id` requis.")
    if inp.op == "grant":
        return users_admin._grant_org_key(ctx, users_admin.OrgGrantKeyInput(
            org_id=org_id, key_id=key_id, daily_quota=inp.daily_quota))
    return users_admin._revoke_org_key(ctx, users_admin.OrgRevokeKeyInput(org_id=org_id, key_id=key_id))


CAPABILITIES += [
    Capability(
        key="admin.org", handler=_org, Input=OrgAdminInput,
        authz=ADMIN_BY_OP({"create": SUPER_ADMIN, "archive": SUPER_ADMIN,
                           "list": PLATFORM_ADMIN, "get": PLATFORM_ADMIN}),
        description=("Manage organizations. op=create (`name`, super admin) / archive "
                     "(`org_id`, super admin) / list (all orgs, platform admin) / get "
                     "(`org_id` → full fiche: members, secrets, entitlements, grants; platform admin)."),
        mcp="oto_admin_org",
    ),
    Capability(
        key="admin.org_member", handler=_org_member, Input=OrgMemberAdminInput,
        authz=ADMIN_BY_OP({"add": ORG_ADMIN_OF("org_id"),
                           "remove": ORG_ADMIN_OF("org_id"),
                           "set_role": ORG_ADMIN_OF("org_id"),
                           "list": PLATFORM_ADMIN}),
        description=("Manage an org's members (org_admin of `org_id`; list = platform admin). "
                     "op=add (`target` email|sub, `role` org_member|org_admin) / remove (`target`) / "
                     "set_role (`target`, `role`) / list. Anti-lockout on the last org_admin."),
        mcp="oto_admin_org_member",
    ),
    Capability(
        key="admin.user", handler=_user, Input=UserAdminInput,
        authz=ADMIN_BY_OP({"list": PLATFORM_ADMIN, "get": PLATFORM_ADMIN,
                           "set_role": SUPER_ADMIN}),
        description=("Accounts. op=list (optional `query` email/name/sub; platform admin) / get "
                     "(`target` email|sub → full fiche; platform admin) / set_role (`target`, "
                     "`role` member|admin|super_admin; super admin)."),
        mcp="oto_admin_user",
    ),
    Capability(
        key="admin.access", handler=_access, Input=AccessAdminInput,
        authz=PLATFORM_ADMIN,
        description=("Alpha access gate (platform admin). op=waitlist (pending signups) / grant "
                     "(`sub`, optional `quota` → active + referral quota + email) / reject (`sub` → blocked)."),
        mcp="oto_admin_access",
    ),
    Capability(
        key="admin.key_grant", handler=_key_grant, Input=KeyGrantInput,
        authz=ADMIN_BY_OP({"list": PLATFORM_ADMIN, "grant": SUPER_ADMIN, "revoke": SUPER_ADMIN}),
        description=("Platform keys as a RIGHT — never reveals the secret. "
                     "op=list (which vendors oto contracts: key_id, provider, label; optional "
                     "`provider` filter; platform admin) / grant|revoke (by `key_id`, super admin) · "
                     "scope=user (`target` email|sub) | org (`org_id`); grant takes optional "
                     "`daily_quota`. To POSE a raw key/secret, use the dashboard."),
        mcp="oto_admin_key_grant",
    ),
]
