"""Console org/équipe MCP consolidée (ADR 0047, B3) — fusion `*_op`.

Quatre objets métier : `oto_org` (cycle de vie : create/update/archive +
invitations), `oto_org_settings` (les réglages d'org par domaine : email / mfa /
field_filters / model_subscriptions), `oto_group` (équipes : create/list/membres/guide d'équipe),
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

from . import scheduled_emails
from .connectors import sharing as connectors_sharing
from .groups import core as groups, guide as groups_guide, invites as groups_invites, members as groups_members
from .orgs import core as orgs, email_settings as orgs_email_settings, field_filters as orgs_field_filters, invites as orgs_invites, mfa as orgs_mfa, model_subscriptions as orgs_model_subscriptions, update as orgs_update
from ._authz import (
    BY_OP,
    GROUP_ADMIN_OF,
    GROUP_MEMBER_OF,
    ORG_ADMIN_OF,
    ORG_MEMBER_OF,
    SUB_ONLY,
)
from ..db import org_subscription_pool
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


# ── oto_org : create / update / archive / invite / accept_invite / reject_invite ─
class OrgInput(BaseModel):
    op: Literal["create", "update", "archive", "invite",
                "accept_invite", "reject_invite", "list_invites", "list_loans"]
    org_id: Optional[int] = None           # update/archive/invite/list_invites/list_loans
    name: Optional[str] = None             # create/update
    description: Optional[str] = None      # update
    domain: Optional[str] = None           # update : domaine de marque (logo dérivé)
    industry: Optional[str] = None         # update
    location: Optional[str] = None         # update
    email: Optional[str] = None            # invite : destinataire (None = lien nominatif)
    role: Optional[str] = None             # invite : org_member (défaut) | org_admin
    send_email: bool = True                # invite
    token: Optional[str] = None            # accept_invite / reject_invite
    carrier: Optional[str] = None          # accept_invite


def _org(ctx: ResolvedCtx, inp: OrgInput) -> dict:
    if inp.op == "create":
        return orgs._create_org(ctx, orgs.CreateOrgInput(
            name=_need(inp.name, "missing_name", "`name` requis pour create.")))
    if inp.op == "accept_invite":
        return orgs_invites._invite_accept(ctx, orgs_invites.InviteAcceptInput(
            token=inp.token, carrier=inp.carrier))
    if inp.op == "reject_invite":
        # Même handler que `POST /api/me/invitations/reject` — pas une copie : les
        # deux faces doivent refuser aux mêmes conditions (#654).
        return orgs_invites._invite_reject(ctx, orgs_invites.InviteRejectInput(
            token=inp.token))
    oid = _need(inp.org_id, "missing_org", f"`org_id` requis pour {inp.op}.")
    if inp.op == "update":
        return orgs_update._update_org(ctx, orgs_update.UpdateOrgInput(
            org_id=oid, name=inp.name, description=inp.description,
            domain=inp.domain, industry=inp.industry, location=inp.location))
    if inp.op == "archive":
        return orgs_update._archive_org(ctx, orgs_update.OrgIdInput(org_id=oid))
    if inp.op == "list_invites":
        return orgs_invites._invite_list(ctx, orgs_invites.InviteListInput(org_id=oid))
    if inp.op == "list_loans":
        return connectors_sharing._list_org_loans(
            ctx, connectors_sharing.OrgLoansInput(org_id=oid))
    return orgs_invites._invite_create(ctx, orgs_invites.InviteCreateInput(
        org_id=oid, email=inp.email, role=inp.role or "org_member",
        send_email=inp.send_email))


# ── oto_org_settings : get / set / preview × domaine email|mfa|field_filters|model_subscriptions ─
class OrgSettingsInput(BaseModel):
    op: Literal["get", "set", "preview"]
    domain: Literal["email", "mfa", "field_filters", "model_subscriptions", "time_saved"]
    org_id: int
    # email (set) :
    connector: Optional[str] = None          # scaleway | resend
    senders: Optional[list[dict]] = None     # [{email, name?, reply_to?}]
    quiet_hours: Optional[dict] = None       # {tz, start, end}
    clear_quiet_hours: bool = False
    footer: Optional[dict] = None            # {unsubscribe_url?, unsubscribe_email?}
    clear_footer: bool = False
    # mfa (set) :
    require: Optional[bool] = None
    # field_filters :
    include_schemas: bool = False            # get
    service: Optional[str] = None            # set/preview
    rules: Optional[list[dict]] = None       # set/preview (None efface au set)
    salt: Optional[str] = None               # set/preview
    unmask: Optional[list[str]] = None       # set : champs du plancher laissés en clair
    documents: Optional[bool] = None         # set : servir les documents du service
    payload: Any = None                      # preview : échantillon réel
    # model_subscriptions :
    family: Optional[str] = None             # get/set : claude_subscription
    limit_pct: Optional[int] = None          # set : 1..100, null (EXPLICITE) = défaut
    mode: Optional[str] = None               # set : personnel | pool (seul, sans limit_pct)
    api_fallback: Optional[bool] = None      # set : repli clé de l'org (seul) ; défaut d'instance
    # time_saved (set) : minutes par type de résultat — {"record": 2, …} ; une
    # valeur null rend le défaut à ce type, `{}` les rend tous.
    minutes: Optional[dict[str, Optional[float]]] = None


def _temps_gagne(ctx: ResolvedCtx, inp: OrgSettingsInput) -> dict:
    """Les minutes par type de résultat du temps gagné des agents : les défauts de la
    plateforme (`time_saved.KINDS`) sont des ESTIMATIONS, et l'org leur substitue les
    siennes. Rien n'est à poser pour avoir un chiffre — c'est un réglage, jamais une
    étape."""
    from .. import db, time_saved
    if inp.op not in ("get", "set"):
        raise AuthzDenied(400, "unsupported_op", "preview n'existe que pour field_filters.")
    if inp.op == "set":
        if inp.minutes is None:
            raise AuthzDenied(400, "missing_minutes",
                              "`minutes` requis pour set : {type: minutes|null}, `{}` rend les défauts.")
        inconnus = sorted(set(inp.minutes) - set(time_saved.KINDS))
        if inconnus:
            raise AuthzDenied(400, "unknown_kind",
                              f"Types inconnus : {', '.join(inconnus)} — connus : "
                              f"{', '.join(time_saved.KINDS)}. Rien n'a été modifié.")
        hors = sorted(k for k, v in inp.minutes.items()
                      if v is not None and not time_saved.OVERRIDE_MIN <= v <= time_saved.OVERRIDE_MAX)
        if hors:
            raise AuthzDenied(400, "minutes_out_of_range",
                              f"Entre {time_saved.OVERRIDE_MIN:g} et {time_saved.OVERRIDE_MAX:g} "
                              f"minutes par résultat : {', '.join(hors)}. Rien n'a été modifié.")
        actuel = {} if inp.minutes == {} else db.minutes_gagnees_de_l_org(inp.org_id)
        for k, v in inp.minutes.items():
            if v is None:
                actuel.pop(k, None)
            else:
                actuel[k] = float(v)
        if not db.poser_minutes_gagnees(inp.org_id, actuel or None):
            raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    surcharges = db.minutes_gagnees_de_l_org(inp.org_id)
    appliquees = time_saved.standards(surcharges)
    return {"org_id": inp.org_id,
            "kinds": [{"kind": k, "label": v["label"], "default_minutes": v["minutes"],
                       "minutes": appliquees[k], "overridden": k in surcharges}
                      for k, v in time_saved.KINDS.items()],
            "research_cap_per_run": time_saved.RESEARCH_CAP_PER_RUN}


def _org_settings(ctx: ResolvedCtx, inp: OrgSettingsInput) -> dict:
    es, mfa, ff = orgs_email_settings, orgs_mfa, orgs_field_filters
    if inp.domain == "time_saved":
        return _temps_gagne(ctx, inp)
    if inp.domain == "model_subscriptions":
        return _plafond_abonnements(ctx, inp)
    if inp.domain == "email":
        if inp.op == "get":
            return es._get_email_settings(ctx, es.GetEmailSettingsInput(org_id=inp.org_id))
        if inp.op == "set":
            return es._set_email_settings(ctx, es.SetEmailSettingsInput(
                org_id=inp.org_id,
                connector=_need(inp.connector, "missing_connector",
                                "`connector` (scaleway|resend) requis pour set."),
                senders=inp.senders, quiet_hours=inp.quiet_hours,
                clear_quiet_hours=inp.clear_quiet_hours,
                footer=inp.footer, clear_footer=inp.clear_footer))
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
            org_id=inp.org_id, service=service, rules=inp.rules, salt=inp.salt,
            unmask=inp.unmask, documents=inp.documents))
    if inp.payload is None:
        raise AuthzDenied(400, "missing_payload",
                          "`payload` (échantillon de réponse réel) requis pour preview.")
    return ff._preview_field_filter(ctx, ff.PreviewFieldFilterInput(
        org_id=inp.org_id, service=service, payload=inp.payload,
        rules=inp.rules, salt=inp.salt))


def _plafond_abonnements(ctx: ResolvedCtx, inp: OrgSettingsInput) -> dict:
    ms = orgs_model_subscriptions
    family = _need(inp.family, "missing_family",
                   f"`family` (ex. claude_subscription) requis pour {inp.op}.")
    if inp.op == "get":
        return ms._get_plafond(ctx, ms.GetOrgPlafondInput(org_id=inp.org_id, family=family))
    if inp.op == "set" and inp.api_fallback is not None:
        # Même discipline que `mode` : un réglage par appel. Trois interrupteurs sur
        # la même ressource, et un `set` qui en porterait deux ne dirait pas lequel
        # on voulait laisser tel quel.
        if inp.mode is not None or inp.limit_pct is not None:
            raise AuthzDenied(400, "one_setting_per_call",
                              "`api_fallback` se règle seul : un appel par réglage.")
        return ms._set_repli(ctx, ms.SetOrgRepliInput(
            org_id=inp.org_id, family=family, api_fallback=inp.api_fallback))
    if inp.op == "set" and inp.mode is not None:
        # Le MODE se règle SEUL. Sur cette face, un `limit_pct` omis et un `null`
        # arrivent identiques (fastmcp remplit les défauts) : un `set` qui porterait
        # les deux ne dirait pas si le plafond devait revenir au défaut.
        if inp.limit_pct is not None:
            raise AuthzDenied(400, "one_setting_per_call",
                              "`mode` se règle seul : un appel pour le mode, un autre "
                              "pour `limit_pct`.")
        if inp.mode not in org_subscription_pool.MODES:
            raise AuthzDenied(400, "invalid_mode",
                              "`mode` vaut `personnel` ou `pool`.")
        return ms._set_mode(ctx, ms.SetOrgModeInput(
            org_id=inp.org_id, family=family, mode=inp.mode))
    if inp.op == "set":
        # `null` revient au défaut : c'est un GESTE, donc il se dit. Un `set` sans
        # `limit_pct` du tout est une erreur, pas un retour silencieux au défaut.
        if "limit_pct" not in inp.model_fields_set:
            raise AuthzDenied(400, "missing_limit_pct",
                              "`limit_pct` (1..100, ou null pour le défaut) requis pour set.")
        return ms._set_plafond(ctx, ms.SetOrgPlafondInput(
            org_id=inp.org_id, family=family, limit_pct=inp.limit_pct))
    raise AuthzDenied(400, "unsupported_op", "preview n'existe que pour field_filters.")


# ── oto_group : create / list / add_member / remove_member / set_instruction ─
class GroupInput(BaseModel):
    op: Literal["create", "list", "get", "add_member", "remove_member", "set_instruction",
                "invite", "list_invites"]
    org_id: Optional[int] = None           # create
    group_id: Optional[int] = None         # get/add/remove/set_instruction/invite/list_invites
    name: Optional[str] = None             # create
    description: Optional[str] = None      # create / set_instruction
    target: Optional[str] = None           # add/remove : sub ou email
    role: Optional[str] = None             # add/invite : group_member (défaut) | group_admin
    email: Optional[str] = None            # invite : destinataire (None = lien à partager)
    send_email: bool = True                # invite
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
    if inp.op == "get":
        # La fiche que sert la console (`GET /api/groups/{id}`) : membres et comptes
        # partagés ou prêtés à l'équipe — le même handler, pas une copie.
        return groups._group_detail(ctx, groups.GroupIdInput(group_id=gid))
    if inp.op == "list_invites":
        return groups_invites._invite_list(
            ctx, groups_invites.GroupInviteListInput(group_id=gid))
    if inp.op == "add_member":
        return groups_members._add_member(ctx, groups_members.AddGroupMemberInput(
            group_id=gid,
            target=_need(inp.target, "missing_target", "`target` (sub ou email) requis."),
            role=inp.role or "group_member"))
    if inp.op == "remove_member":
        return groups_members._remove_member(ctx, groups_members.RemoveGroupMemberInput(
            group_id=gid,
            target=_need(inp.target, "missing_target", "`target` (sub ou email) requis.")))
    if inp.op == "invite":
        return groups_invites._invite_create(ctx, groups_invites.GroupInviteCreateInput(
            group_id=gid, email=inp.email, role=inp.role or "group_member",
            send_email=inp.send_email))
    return groups_guide._set(ctx, groups_guide.InstrSetInput(
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
            # Refuser est le geste de l'INVITÉ, pas d'un administrateur : SUB_ONLY,
            # et c'est le handler qui confronte l'adresse invitée à celle du compte
            # (le refus n'est pas bearer, contrairement à l'acceptation — #654).
            "reject_invite": SUB_ONLY,
            "update": ORG_ADMIN_OF("org_id"), "archive": ORG_ADMIN_OF("org_id"),
            "invite": ORG_ADMIN_OF("org_id"),
            "list_invites": ORG_ADMIN_OF("org_id"), "list_loans": ORG_ADMIN_OF("org_id"),
        }),
        refresh_visibility=True,   # create/archive changent l'org effective → toolbox
        description=(
            "Org lifecycle. op=create (`name` — you become its admin, it becomes active) / "
            "update (`org_id` + name/description/domain/industry/location; empty string "
            "clears a field) / archive (`org_id`) / invite (`org_id`, optional `email` + "
            "`role` org_member|org_admin, send_email=false returns the link only) / "
            "accept_invite (`token`) / reject_invite (`token` — declines it without "
            "joining; only the invited address may decline). Org admin, on `org_id`: "
            "list_invites (pending invitations) / list_loans (accounts members lend: "
            "who, which account and services, to whom, since when). "
            "To switch org, use oto_use_org."),
        mcp="oto_org",
    ),
    Capability(
        key="org.settings.console", handler=_org_settings, Input=OrgSettingsInput,
        authz=BY_OP({"get": ORG_MEMBER_OF("org_id"), "set": ORG_ADMIN_OF("org_id"),
                     "preview": ORG_MEMBER_OF("org_id")}),
        description=(
            "Org settings, by domain. domain=email (per-connector senders + quiet hours: "
            "set takes `connector` scaleway|resend, `senders` [{email,name?,reply_to?}], "
            "`quiet_hours` {tz,start,end} or clear_quiet_hours=true, `footer` "
            "{unsubscribe_url?,unsubscribe_email?} = the org's own unsubscribe — once "
            "declared it REPLACES the platform footer on that connector's sends, refused "
            "without either field; clear_footer=true restores ours) | mfa (set `require` "
            "true|false — org-wide mandatory MFA) | field_filters (redaction policy ADR "
            "0015: get returns policies, include_schemas=true adds the observed field "
            "catalog; set takes `service` + `rules` (None clears) + optional `salt`; "
            "a server-default (floor) field leaves masking only when NAMED in "
            "`unmask` [field, …] — `rules: []` alone lifts nothing — and the service's "
            "documents are served only with `documents: true`; "
            "op=preview dry-runs rules against a real `payload` sample) | model_subscriptions "
            "(the org's consumption cap on members' personal model subscriptions, `family` "
            "e.g. claude_subscription: get returns `limit_pct` + `default`; set takes "
            "`limit_pct` 1..100 = max % of each member's provider account TOTAL usage "
            "(5-hour and 7-day windows) before the org's next jobs wait for the reset, or "
            "null = back to the default 80; a member's own cap (1..95) replaces it for "
            "them; OR, in a separate call, `mode` personnel|pool — `pool` runs the "
            "org's jobs, fleets included, ONLY on subscriptions members explicitly lent to "
            "the org, least recently used first; unset, lent subscriptions complement: "
            "they serve members with no usable subscription of their own, and fleets; OR, in a separate call, `api_fallback` "
            "true|false — true lets a job whose subscription is exhausted replay on the "
            "org's OWN deposited API key, same tier, instead of waiting (the default depends on the instance); "
            "get also returns `mode`, `pool_size`, `lenders` (who lends, and whether each is "
            "usable now — never their tier or usage) and `api_fallback`) | time_saved "
            "(minutes per result type behind the agents' time saved — report, action, record, "
            "research; the defaults are estimates, the org may override them; get returns "
            "each kind's default and applied minutes; set takes "
            "`minutes` {kind: 0..240 | null} — null restores that kind's default, `{}` all). "
            "op=get is member, set is org admin."),
        mcp="oto_org_settings",
    ),
    Capability(
        key="group.console", handler=_group, Input=GroupInput,
        authz=BY_OP({
            "create": ORG_ADMIN_OF("org_id"), "list": SUB_ONLY,
            "get": GROUP_MEMBER_OF("group_id"),
            "list_invites": GROUP_ADMIN_OF("group_id"),
            "add_member": GROUP_ADMIN_OF("group_id"),
            "remove_member": GROUP_ADMIN_OF("group_id"),
            "invite": GROUP_ADMIN_OF("group_id"),
            "set_instruction": GROUP_ADMIN_OF("group_id"),
        }),
        description=(
            "Teams (departments) of an org. op=create (`org_id`, `name`; org admin) / list "
            "(the teams of your active org + your role in each) / get (`group_id`: its "
            "members and the accounts shared or lent to the team; a member, or an org "
            "admin) / list_invites (`group_id`, pending invitations; team lead) / add_member (`group_id`, `target` sub|email, optional "
            "`role` group_member|group_admin; team lead — target must already be in the org) / "
            "invite (`group_id`, optional `email` + `role` group_member|group_admin, "
            "send_email=false returns the link — invites a NEW person into the team, joining "
            "the org too on accept) / remove_member (`group_id`, `target`) / set_instruction "
            "(`group_id`, `slug`, `body_md` — the team's guide, served on top of the "
            "org's). To switch team, use oto_use_group. Team shared secrets are set on the "
            "dashboard (never via MCP)."),
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
