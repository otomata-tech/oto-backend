"""Console org/équipe MCP consolidée (ADR 0047, B3) — fusion `*_op`.

Quatre objets métier : `oto_org` (cycle de vie : create/update/archive +
invitations), `oto_org_settings` (les réglages d'org par domaine : email / mfa /
field_filters), `oto_group` (équipes : create/list/membres/doctrine d'équipe),
`oto_scheduled_emails` (file d'envoi différé). Handlers de domaine réutilisés
tels quels, faces REST intactes.

Hors périmètre (échappatoires anti-lockout, jamais fusionnées) : `oto_use_org`/
`oto_clear_org`/`oto_list_orgs`/`oto_use_group`/`oto_clear_group`. Retrait sec :
`oto_set_group_secret` perd sa face MCP (secret brut jamais en argument MCP —
même règle que les secrets d'org, pose dashboard-only).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

from . import (
    groups,
    groups_doctrine,
    groups_members,
    orgs,
    orgs_email_settings,
    orgs_field_filters,
    orgs_invites,
    orgs_mfa,
    orgs_update,
    scheduled_emails,
)
from ._authz import (
    BY_OP,
    GROUP_ADMIN_OF,
    ORG_ADMIN_OF,
    ORG_MEMBER_OF,
    SUB_ONLY,
)
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


# ── oto_org : create / update / archive / invite / accept_invite ─────────────
class OrgInput(BaseModel):
    op: Literal["create", "update", "archive", "invite", "accept_invite"]
    org_id: Optional[int] = None           # update/archive/invite
    name: Optional[str] = None             # create/update
    description: Optional[str] = None      # update
    domain: Optional[str] = None           # update : domaine de marque (logo dérivé)
    industry: Optional[str] = None         # update
    location: Optional[str] = None         # update
    email: Optional[str] = None            # invite : destinataire (None = lien nominatif)
    role: Optional[str] = None             # invite : org_member (défaut) | org_admin
    send_email: bool = True                # invite
    token: Optional[str] = None            # accept_invite
    code: Optional[str] = None             # accept_invite
    carrier: Optional[str] = None          # accept_invite


def _org(ctx: ResolvedCtx, inp: OrgInput) -> dict:
    if inp.op == "create":
        return orgs._create_org(ctx, orgs.CreateOrgInput(
            name=_need(inp.name, "missing_name", "`name` requis pour create.")))
    if inp.op == "accept_invite":
        return orgs_invites._invite_accept(ctx, orgs_invites.InviteAcceptInput(
            token=inp.token, code=inp.code, carrier=inp.carrier))
    oid = _need(inp.org_id, "missing_org", f"`org_id` requis pour {inp.op}.")
    if inp.op == "update":
        return orgs_update._update_org(ctx, orgs_update.UpdateOrgInput(
            org_id=oid, name=inp.name, description=inp.description,
            domain=inp.domain, industry=inp.industry, location=inp.location))
    if inp.op == "archive":
        return orgs_update._archive_org(ctx, orgs_update.OrgIdInput(org_id=oid))
    return orgs_invites._invite_create(ctx, orgs_invites.InviteCreateInput(
        org_id=oid, email=inp.email, role=inp.role or "org_member",
        send_email=inp.send_email))


# ── oto_org_settings : get / set / preview × domaine email|mfa|field_filters ─
class OrgSettingsInput(BaseModel):
    op: Literal["get", "set", "preview"]
    domain: Literal["email", "mfa", "field_filters"]
    org_id: int
    # email (set) :
    connector: Optional[str] = None          # scaleway | resend
    senders: Optional[list[dict]] = None     # [{email, name?, reply_to?}]
    quiet_hours: Optional[dict] = None       # {tz, start, end}
    clear_quiet_hours: bool = False
    # mfa (set) :
    require: Optional[bool] = None
    # field_filters :
    include_schemas: bool = False            # get
    service: Optional[str] = None            # set/preview
    rules: Optional[list[dict]] = None       # set/preview (None efface au set)
    salt: Optional[str] = None               # set/preview
    payload: Any = None                      # preview : échantillon réel


def _org_settings(ctx: ResolvedCtx, inp: OrgSettingsInput) -> dict:
    es, mfa, ff = orgs_email_settings, orgs_mfa, orgs_field_filters
    if inp.domain == "email":
        if inp.op == "get":
            return es._get_email_settings(ctx, es.GetEmailSettingsInput(org_id=inp.org_id))
        if inp.op == "set":
            return es._set_email_settings(ctx, es.SetEmailSettingsInput(
                org_id=inp.org_id,
                connector=_need(inp.connector, "missing_connector",
                                "`connector` (scaleway|resend) requis pour set."),
                senders=inp.senders, quiet_hours=inp.quiet_hours,
                clear_quiet_hours=inp.clear_quiet_hours))
        raise AuthzDenied(400, "unsupported_op", "preview n'existe que pour field_filters.")
    if inp.domain == "mfa":
        if inp.op == "get":
            return mfa._get_org_mfa(ctx, mfa.GetOrgMfaInput(org_id=inp.org_id))
        if inp.op == "set":
            if inp.require is None:
                raise AuthzDenied(400, "missing_require", "`require` requis pour set.")
            return mfa._set_org_mfa(ctx, mfa.SetOrgMfaInput(org_id=inp.org_id, require=inp.require))
        raise AuthzDenied(400, "unsupported_op", "preview n'existe que pour field_filters.")
    if inp.op == "get":
        return ff._get_field_filters(ctx, ff.GetFieldFiltersInput(
            org_id=inp.org_id, include_schemas=inp.include_schemas))
    service = _need(inp.service, "missing_service", f"`service` requis pour {inp.op}.")
    if inp.op == "set":
        return ff._set_field_filter(ctx, ff.SetFieldFilterInput(
            org_id=inp.org_id, service=service, rules=inp.rules, salt=inp.salt))
    if inp.payload is None:
        raise AuthzDenied(400, "missing_payload",
                          "`payload` (échantillon de réponse réel) requis pour preview.")
    return ff._preview_field_filter(ctx, ff.PreviewFieldFilterInput(
        org_id=inp.org_id, service=service, payload=inp.payload,
        rules=inp.rules, salt=inp.salt))


# ── oto_group : create / list / add_member / remove_member / set_instruction ─
class GroupInput(BaseModel):
    op: Literal["create", "list", "add_member", "remove_member", "set_instruction"]
    org_id: Optional[int] = None           # create
    group_id: Optional[int] = None         # add/remove/set_instruction
    name: Optional[str] = None             # create
    description: Optional[str] = None      # create / set_instruction
    target: Optional[str] = None           # add/remove : sub ou email
    role: Optional[str] = None             # add : group_member (défaut) | group_admin
    slug: Optional[str] = None             # set_instruction
    body_md: Optional[str] = None          # set_instruction
    title: Optional[str] = None            # set_instruction


def _group(ctx: ResolvedCtx, inp: GroupInput) -> dict:
    if inp.op == "create":
        return groups._create_group(ctx, groups.CreateGroupInput(
            org_id=_need(inp.org_id, "missing_org", "`org_id` requis pour create."),
            name=_need(inp.name, "missing_name", "`name` requis pour create."),
            description=inp.description or ""))
    if inp.op == "list":
        # Sémantique de l'ex-oto_list_groups (group.list_mine) : les équipes de TON org
        # active + ton rôle. La lecture par org_id explicite reste REST (dashboard).
        return groups._list_my_groups(ctx, groups.NoInput())
    gid = _need(inp.group_id, "missing_group", f"`group_id` requis pour {inp.op}.")
    if inp.op == "add_member":
        return groups_members._add_member(ctx, groups_members.AddGroupMemberInput(
            group_id=gid,
            target=_need(inp.target, "missing_target", "`target` (sub ou email) requis."),
            role=inp.role or "group_member"))
    if inp.op == "remove_member":
        return groups_members._remove_member(ctx, groups_members.RemoveGroupMemberInput(
            group_id=gid,
            target=_need(inp.target, "missing_target", "`target` (sub ou email) requis.")))
    return groups_doctrine._set(ctx, groups_doctrine.InstrSetInput(
        group_id=gid,
        slug=_need(inp.slug, "missing_slug", "`slug` requis pour set_instruction."),
        body_md=_need(inp.body_md, "missing_body", "`body_md` requis pour set_instruction."),
        title=inp.title, description=inp.description))


# ── oto_scheduled_emails : list / cancel ─────────────────────────────────────
class ScheduledEmailsInput(BaseModel):
    op: Literal["list", "cancel"]
    org_id: int
    status: str = "pending"                # list : pending|sent|failed|cancelled|all
    email_id: Optional[int] = None         # cancel


def _scheduled_emails(ctx: ResolvedCtx, inp: ScheduledEmailsInput) -> dict:
    se = scheduled_emails
    if inp.op == "list":
        return se._scheduled_list(ctx, se.ScheduledListInput(org_id=inp.org_id, status=inp.status))
    return se._scheduled_cancel(ctx, se.ScheduledCancelInput(
        org_id=inp.org_id,
        email_id=_need(inp.email_id, "missing_email_id", "`email_id` requis pour cancel.")))


CAPABILITIES += [
    Capability(
        key="org.console", handler=_org, Input=OrgInput,
        authz=BY_OP({
            "create": SUB_ONLY, "accept_invite": SUB_ONLY,
            "update": ORG_ADMIN_OF("org_id"), "archive": ORG_ADMIN_OF("org_id"),
            "invite": ORG_ADMIN_OF("org_id"),
        }),
        refresh_visibility=True,   # create/archive changent l'org effective → toolbox
        description=(
            "Org lifecycle. op=create (`name` — you become its admin, it becomes active) / "
            "update (`org_id` + name/description/domain/industry/location; empty string "
            "clears a field) / archive (`org_id`) / invite (`org_id`, optional `email` + "
            "`role` org_member|org_admin, send_email=false returns the link only) / "
            "accept_invite (`token` or `code`). To switch org, use oto_use_org."),
        mcp="oto_org",
    ),
    Capability(
        key="org.settings.console", handler=_org_settings, Input=OrgSettingsInput,
        authz=BY_OP({"get": ORG_MEMBER_OF("org_id"), "set": ORG_ADMIN_OF("org_id"),
                     "preview": ORG_MEMBER_OF("org_id")}),
        description=(
            "Org settings, by domain. domain=email (per-connector senders + quiet hours: "
            "set takes `connector` scaleway|resend, `senders` [{email,name?,reply_to?}], "
            "`quiet_hours` {tz,start,end} or clear_quiet_hours=true) | mfa (set `require` "
            "true|false — org-wide mandatory MFA) | field_filters (redaction policy ADR "
            "0015: get returns policies, include_schemas=true adds the observed field "
            "catalog; set takes `service` + `rules` (None clears) + optional `salt`; "
            "op=preview dry-runs rules against a real `payload` sample). op=get is member, "
            "set is org admin."),
        mcp="oto_org_settings",
    ),
    Capability(
        key="group.console", handler=_group, Input=GroupInput,
        authz=BY_OP({
            "create": ORG_ADMIN_OF("org_id"), "list": SUB_ONLY,
            "add_member": GROUP_ADMIN_OF("group_id"),
            "remove_member": GROUP_ADMIN_OF("group_id"),
            "set_instruction": GROUP_ADMIN_OF("group_id"),
        }),
        description=(
            "Teams (departments) of an org. op=create (`org_id`, `name`; org admin) / list "
            "(the teams of your active org + your role in each) / add_member (`group_id`, `target` sub|email, optional "
            "`role` group_member|group_admin; team lead) / remove_member (`group_id`, "
            "`target`) / set_instruction (`group_id`, `slug`, `body_md` — the team's "
            "doctrine, served on top of the org's). To switch team, use oto_use_group. "
            "Team shared secrets are set on the dashboard (never via MCP)."),
        mcp="oto_group",
    ),
    Capability(
        key="org.scheduled_emails.console", handler=_scheduled_emails, Input=ScheduledEmailsInput,
        authz=ORG_MEMBER_OF("org_id"),
        description=(
            "Deferred email queue of an org (`email_send` with send_at or quiet-hours "
            "guard). op=list (`status` pending|sent|failed|cancelled|all, default pending) "
            "/ cancel (`email_id` — only while still pending)."),
        mcp="oto_scheduled_emails",
    ),
]
