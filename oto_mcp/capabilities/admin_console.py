"""Console admin MCP consolidée par concept (ADR 0009, fusion `*_op`).

Réunit les verbes d'un même objet métier en UN outil `oto_admin_<objet>(op=…)`, au
service du provisioning d'org de bout en bout depuis Claude. L'autz reste DÉCLARÉE
(combinateur op-aware `ADMIN_BY_OP` quand les paliers divergent) — jamais redescendue
dans le handler. Les handlers de domaine sont **réutilisés tels quels** (on construit
leur Input spécifique depuis l'Input consolidé) ; aucune logique n'est dupliquée. Les
faces REST historiques ne bougent pas : on retire seulement le binding `mcp=` des
capacités d'origine.

Concepts : `oto_admin_org`, `oto_admin_org_member`, `oto_admin_user`,
`oto_admin_key_grant`. Hors périmètre (décision 2026-06-25) :
pose de secret brut (`set_org_secret`/`set_platform_key`) = dashboard-only.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import credentials_store, db
from . import orgs_admin, orgs_members, orgs_reads, users_admin
from ._authz import ADMIN_BY_OP, ORG_ADMIN_OF, ORG_MEMBER_OF, PLATFORM_ADMIN, SUPER_ADMIN
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


# ── oto_admin_key_grant : list / grant / revoke · scope user|org (DROITS, pas de secret) ─
class KeyGrantInput(BaseModel):
    op: Literal["list", "grant", "revoke"]
    scope: Optional[Literal["user", "org"]] = None  # grant/revoke seulement
    target: Optional[str] = None      # scope=user : email ou sub
    org_id: Optional[int] = None      # scope=org
    provider: Optional[str] = None    # grant/revoke : connecteur ciblé ; op=list : filtre
    daily_quota: Optional[int] = None  # grant (optionnel)


def _key_grant(ctx: ResolvedCtx, inp: KeyGrantInput) -> dict:
    if inp.op == "list":
        # Inventaire des clés plateforme posées (quels vendors oto contracte). Le
        # SECRET n'est JAMAIS renvoyé — on ne montre que l'identité (provider, label).
        # ADR 0044 §F : instances scope PLATFORM du coffre unifié (plus platform_keys).
        keys = credentials_store.list_platform_credentials(inp.provider)
        return {"keys": keys, "count": len(keys)}
    scope = _need(inp.scope, "missing_scope", "`scope` (user|org) requis pour grant/revoke.")
    provider = _need(inp.provider, "missing_provider", "`provider` (connecteur) requis.")
    if inp.scope == "user":
        target = _need(inp.target, "missing_target", "scope=user : `target` requis.")
        if inp.op == "grant":
            return users_admin._grant_key(ctx, users_admin.GrantKeyInput(
                target=target, provider=provider, daily_quota=inp.daily_quota))
        return users_admin._revoke_key(ctx, users_admin.RevokeKeyInput(target=target, provider=provider))
    org_id = _need(inp.org_id, "missing_org", "scope=org : `org_id` requis.")
    if inp.op == "grant":
        return users_admin._grant_org_key(ctx, users_admin.OrgGrantKeyInput(
            org_id=org_id, provider=provider, daily_quota=inp.daily_quota))
    return users_admin._revoke_org_key(ctx, users_admin.OrgRevokeKeyInput(org_id=org_id, provider=provider))


# ── oto_admin_doctrine : get / list / set / delete (org ciblée, ADR 0047 B2) ─
class DoctrineAdminInput(BaseModel):
    op: Literal["get", "list", "set", "delete"]
    org_id: int
    slug: Optional[str] = None        # get (None = base+index) / set (None = base) / delete
    scope: Optional[str] = None       # get/list : org (défaut) | group
    version: Optional[int] = None     # get
    with_history: bool = False        # get
    query: Optional[str] = None       # list
    body_md: Optional[str] = None     # set
    title: Optional[str] = None       # set
    description: Optional[str] = None  # set
    from_version: Optional[int] = None  # set (revert)
    slots: Optional[list] = None      # set (ADR 0035)


async def _doctrine(ctx: ResolvedCtx, inp: DoctrineAdminInput) -> dict:
    from . import orgs_instructions as oi
    if inp.op == "get":
        return await oi._get_doctrine(ctx, oi.AdminDoctrineGetInput(
            org_id=inp.org_id, slug=inp.slug, scope=inp.scope or "org",
            version=inp.version, with_history=inp.with_history))
    if inp.op == "list":
        return oi._list_doctrines(ctx, oi.AdminDoctrineListInput(
            org_id=inp.org_id, query=inp.query, scope=inp.scope))
    if inp.op == "set":
        return await oi._set_instruction(ctx, oi.AdminInstrSetInput(
            org_id=inp.org_id, slug=inp.slug, body_md=inp.body_md, title=inp.title,
            description=inp.description, from_version=inp.from_version, slots=inp.slots))
    return oi._delete_instruction(ctx, oi.AdminSlugInput(
        org_id=inp.org_id,
        slug=_need(inp.slug, "missing_slug", "`slug` requis pour delete.")))


# ── oto_admin_signal : list / resolve (boucle d'usage, ADR 0017) ─────────────
class SignalAdminInput(BaseModel):
    op: Literal["list", "resolve"]
    signal: Optional[str] = None      # list : tool_feedback | gap
    target: Optional[str] = None      # list
    status: Optional[str] = None      # list : open | resolved | None (tous)
    limit: int = 200                  # list
    signal_id: Optional[int] = None   # resolve
    note: Optional[str] = None        # resolve
    resolved: bool = True             # resolve : False = ré-ouvrir


def _signal(ctx: ResolvedCtx, inp: SignalAdminInput) -> dict:
    from . import usage
    if inp.op == "list":
        return usage._signals(ctx, usage.SignalsInput(
            signal=inp.signal, target=inp.target, status=inp.status, limit=inp.limit))
    return usage._resolve_signal(ctx, usage.ResolveSignalInput(
        signal_id=_need(inp.signal_id, "missing_signal_id", "`signal_id` requis pour resolve."),
        note=inp.note, resolved=inp.resolved))


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
        key="admin.key_grant", handler=_key_grant, Input=KeyGrantInput,
        authz=ADMIN_BY_OP({"list": PLATFORM_ADMIN, "grant": SUPER_ADMIN, "revoke": SUPER_ADMIN}),
        description=("Platform keys as a RIGHT — never reveals the secret. "
                     "op=list (which vendors oto contracts: provider, label; optional "
                     "`provider` filter; platform admin) / grant|revoke (by `provider`, super admin) · "
                     "scope=user (`target` email|sub) | org (`org_id`); grant takes optional "
                     "`daily_quota`. To POSE a raw key/secret, use the dashboard."),
        mcp="oto_admin_key_grant",
    ),
    Capability(
        key="admin.doctrine", handler=_doctrine, Input=DoctrineAdminInput,
        authz=ADMIN_BY_OP({"get": ORG_MEMBER_OF("org_id"), "list": ORG_MEMBER_OF("org_id"),
                           "set": ORG_ADMIN_OF("org_id"), "delete": ORG_ADMIN_OF("org_id")}),
        description=("[ADMIN] Another org's doctrine, by `org_id` (cross-org = platform "
                     "admin). op=get (`slug` = one skill, none = base+index; `scope=group`) "
                     "/ list (named doctrines incl. base) / set (write: omit slug = base; "
                     "`from_version` restores; `slots` = required entities ADR 0035) / "
                     "delete (exact `slug`, drops history)."),
        mcp="oto_admin_doctrine",
    ),
    Capability(
        key="admin.signal", handler=_signal, Input=SignalAdminInput,
        authz=PLATFORM_ADMIN,
        description=("Usage signals reported about oto (feedback/gap; platform admin). "
                     "op=list (most recent first; filters `signal` tool_feedback|gap, "
                     "`target`, `status` open|resolved) / resolve (`signal_id`, optional "
                     "`note` = what was done; resolved=false re-opens)."),
        mcp="oto_admin_signal",
    ),
]
