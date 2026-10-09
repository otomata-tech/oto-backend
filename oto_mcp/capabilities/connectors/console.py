"""Consolidated MCP connectors console (ADR 0047, B1) — `*_op` merge.

Gathers the MCP tools of the connectors family into 5, one per business object,
verb in the `op` param (+ `scope` org|team when the grain exists at both
levels) — the admin console pattern (`admin_console.py`) applied to the
non-admin surface. Authz remains DECLARED (`BY_OP` combinator, key `(op, scope)`
when the tier depends on both); the domain handlers are reused as
is (we build their specific Input); the REST faces of the original
capabilities do not move — only their `mcp=` binding is removed.

Concepts: `oto_connector_activation` (org/team exposure), `oto_connector`
(marketplace + org acts: force/recommend), `oto_instance` (instances
ADR 0038/0044: list/lend/verify), `oto_identity` (identity selector
ADR 0024), `oto_account_access` (shared accounts #55).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import (account_grants as connectors_account_grants,
               activation as connectors_activation,
               force as connectors_force,
               identities as connectors_identities,
               instances as connectors_instances,
               selection as connectors_selection,
               sharing as connectors_sharing,
               verify as connectors_verify)
from .._authz import (
    BY_OP,
    GROUP_ADMIN_OF,
    GROUP_MEMBER_OF,
    ORG_ADMIN_OF,
    ORG_MEMBER,
    ORG_MEMBER_OF,
    SUB_ONLY,
)
from .._types import AuthzDenied, Capability, ResolvedCtx
from ..registry import CAPABILITIES


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


# ── oto_connector_activation : list / set / clear · scope org|group ──────────
class ActivationInput(BaseModel):
    op: Literal["list", "set", "clear"]
    scope: Literal["org", "group"] = "org"
    org_id: Optional[int] = None       # scope=org
    group_id: Optional[int] = None     # scope=group
    name: Optional[str] = None         # set/clear: connector
    enabled: Optional[bool] = None     # set


def _activation(ctx: ResolvedCtx, inp: ActivationInput) -> dict:
    a = connectors_activation
    if inp.scope == "org":
        oid = _need(inp.org_id, "missing_org", "`org_id` is required for scope=org.")
        if inp.op == "list":
            return a._org_list(ctx, a.OrgActivationListInput(org_id=oid))
        name = _need(inp.name, "missing_name", f"`name` (connector) is required for {inp.op}.")
        if inp.op == "set":
            if inp.enabled is None:
                raise AuthzDenied(400, "missing_enabled", "`enabled` is required for set.")
            return a._org_set(ctx, a.OrgActivationSetInput(org_id=oid, name=name, enabled=inp.enabled))
        return a._org_clear(ctx, a.OrgActivationClearInput(org_id=oid, name=name))
    gid = _need(inp.group_id, "missing_group", "`group_id` is required for scope=group.")
    if inp.op == "list":
        return a._group_list(ctx, a.GroupActivationListInput(group_id=gid))
    name = _need(inp.name, "missing_name", f"`name` (connector) is required for {inp.op}.")
    if inp.op == "set":
        if inp.enabled is None:
            raise AuthzDenied(400, "missing_enabled", "`enabled` is required for set.")
        return a._group_set(ctx, a.GroupActivationSetInput(group_id=gid, name=name, enabled=inp.enabled))
    return a._group_clear(ctx, a.GroupActivationClearInput(group_id=gid, name=name))


# ── oto_connector : list / select / pause / unselect · force / recommend ─────
class ConnectorInput(BaseModel):
    op: Literal["list", "select", "pause", "unselect", "force", "recommend"]
    name: Optional[str] = None                 # list (filter 1 connector) · select/pause/unselect/force
    verbose: bool = False                      # list
    state: Optional[str] = None                # list: not_selected|active|paused
    org_id: Optional[int] = None               # force/recommend
    member: Optional[str] = None               # force: sub or email
    connectors: Optional[list[str]] = None     # recommend: baseline ([] clears)


async def _connector(ctx: ResolvedCtx, inp: ConnectorInput) -> dict:
    # `async` for `force` only: the other actions read/write the database
    # synchronously (`_me`, `_select`…, `_recommend` → `connectors.kit.appliquer`), so never
    # on the loop. Found by the execution guard, not by the sweep: the local alias
    # `sel = connectors_selection` hid the calls.
    sel = connectors_selection
    if inp.op == "list":
        # `name` is honored HERE too (feedback #326): it was declared on the tool
        # but only select/pause/unselect/force read it → passed on list it was silently
        # dropped and the agent received the whole catalog.
        return await run_in_threadpool(sel._me, ctx, sel.MyConnectorsInput(
            verbose=inp.verbose, state=inp.state, name=inp.name))
    if inp.op in ("select", "pause", "unselect"):
        action = sel.ConnectorActionInput(
            name=_need(inp.name, "missing_name", f"`name` (connector) is required for {inp.op}."))
        geste = {"select": sel._select, "pause": sel._pause, "unselect": sel._unselect}[inp.op]
        return await run_in_threadpool(geste, ctx, action)
    oid = _need(inp.org_id, "missing_org", f"`org_id` is required for {inp.op}.")
    if inp.op == "force":
        return await connectors_force._force_connector(ctx, connectors_force.ForceConnectorInput(
            org_id=oid,
            connector=_need(inp.name, "missing_name", "`name` (connector) is required for force."),
            member=_need(inp.member, "missing_member", "`member` (sub or email) is required for force.")))
    if inp.connectors is None:
        raise AuthzDenied(400, "missing_connectors",
                          "`connectors` (list of names, [] to clear) is required for recommend.")
    return await run_in_threadpool(
        sel._recommend, ctx, sel.RecommendInput(org_id=oid, connectors=inp.connectors))


# ── oto_instance : list / lend / verify (ADR 0038 §B, 0044 share_side) ───────
class InstanceInput(BaseModel):
    op: Literal["list", "lend", "verify"]
    connector: Optional[str] = None
    # list: filter member|group|org|platform · verify: auto (effective credential) | org
    level: Optional[str] = None
    to: Optional[str] = None                   # lend: peer's email (org member) or sub
    to_group: Optional[int] = None             # lend: team id (instead of `to`)
    account: str = ""                          # lend
    revoke: bool = False                       # lend: True = take the loan back


async def _instance(ctx: ResolvedCtx, inp: InstanceInput) -> dict:
    if inp.op == "list":
        if inp.level not in (None, "member", "group", "org", "tenant", "platform"):
            raise AuthzDenied(400, "invalid_level",
                              "op=list: `level` ∈ member|group|org|tenant|platform.")
        return connectors_instances._list_instances(
            ctx, connectors_instances.ListInstancesInput(connector=inp.connector, level=inp.level))
    connector = _need(inp.connector, "missing_connector", f"`connector` is required for {inp.op}.")
    if inp.op == "lend":
        if inp.to is None and inp.to_group is None:
            raise AuthzDenied(400, "missing_to", "`to` (the email or the sub of a member of "
                                                 "your organization) or `to_group` (a team "
                                                 "id) is required for lend.")
        return connectors_sharing._lend_instance(ctx, connectors_sharing.LendInstanceInput(
            connector=connector, to=inp.to, to_group=inp.to_group,
            account=inp.account, revoke=inp.revoke))
    if inp.level not in (None, "auto", "org"):
        raise AuthzDenied(400, "invalid_level", "op=verify: `level` ∈ auto|org.")
    return await connectors_verify._verify(
        ctx, connectors_verify.VerifyInput(provider=connector, level=inp.level or "auto"))


# ── oto_identity : list / set (identity selector, ADR 0024) ──────────────────
class IdentityInput(BaseModel):
    op: Literal["list", "set", "rename"]
    connector: str
    identity_id: Optional[str] = None          # set, rename (the current name)
    new_name: Optional[str] = None             # rename
    # Targeted tier: MINE (default), those of my active team, those of my org.
    # The REST face already carried it; the agent face could only see its own —
    # so a member served by their org's key could not know under which
    # accounts it can act, nor which one is the default.
    scope: Literal["member", "org", "group"] = "member"


async def _identity(ctx: ResolvedCtx, inp: IdentityInput) -> dict:
    ids = connectors_identities
    if inp.op == "list":
        return await ids._list(ctx, ids.IdentitiesInput(
            connector=inp.connector, scope=inp.scope))
    if inp.op == "rename":
        return await ids._rename(ctx, ids.RenameIdentityInput(
            connector=inp.connector, scope=inp.scope,
            identity_id=_need(inp.identity_id, "missing_identity",
                              "`identity_id` (the current name) is required for rename."),
            name=_need(inp.new_name, "missing_new_name", "`new_name` is required for rename.")))
    return await ids._set_default(ctx, ids.SetIdentityInput(
        connector=inp.connector, scope=inp.scope,
        identity_id=_need(inp.identity_id, "missing_identity", "`identity_id` is required for set.")))


# ── oto_account_access : list / grant / revoke (shared accounts, #55) ────────
class AccountAccessInput(BaseModel):
    op: Literal["list", "grant", "revoke"]
    channel: Optional[connectors_account_grants.Channel] = None
    grantee: Optional[str] = None              # sub or email


def _account_access(ctx: ResolvedCtx, inp: AccountAccessInput) -> dict:
    ag = connectors_account_grants
    if inp.op == "list":
        return ag._list(ctx, ag.AccountGrantsListInput())
    grant_inp = ag.AccountGrantInput(
        channel=_need(inp.channel, "missing_channel", "`channel` is required."),
        grantee=_need(inp.grantee, "missing_grantee", "`grantee` (sub or email) is required."))
    return ag._grant(ctx, grant_inp) if inp.op == "grant" else ag._revoke(ctx, grant_inp)


CAPABILITIES += [
    Capability(
        key="connectors.console.activation", handler=_activation, Input=ActivationInput,
        authz=BY_OP({
            ("list", "org"): ORG_MEMBER_OF("org_id"),
            ("set", "org"): ORG_ADMIN_OF("org_id"),
            ("clear", "org"): ORG_ADMIN_OF("org_id"),
            ("list", "group"): GROUP_MEMBER_OF("group_id"),
            ("set", "group"): GROUP_ADMIN_OF("group_id"),
            ("clear", "group"): GROUP_ADMIN_OF("group_id"),
        }, fields=("op", "scope")),
        refresh_visibility=True,
        description=(
            "Connector activation governance (org & team cockpit). op=list (each connector's "
            "platform master switch, org override, effective state, recommended) / set "
            "(`name`, `enabled` — org: hard ceiling, enabling requires platform exposure; "
            "team: restrict-only, enabled=true refused) / clear (remove the override, fall "
            "back to the level above). scope=org (`org_id`; list=member, set/clear=org admin) "
            "| group (`group_id`; list=member, set/clear=team lead). Takes effect next session."),
        mcp="oto_connector_activation",
    ),
    Capability(
        key="connectors.console.connector", handler=_connector, Input=ConnectorInput,
        authz=BY_OP({
            "list": SUB_ONLY, "select": SUB_ONLY, "pause": SUB_ONLY, "unselect": SUB_ONLY,
            "force": ORG_ADMIN_OF("org_id"), "recommend": ORG_ADMIN_OF("org_id"),
        }),
        description=(
            "Your connector marketplace + org-level pushes. op=list (catalog with your "
            "per-workspace state not_selected|active|paused + `recommended`; COMPACT rows by "
            "default, verbose=true for the full card, filter with `state` or with `name` to "
            "read ONE connector — do that instead of pulling the whole catalog; `name` also "
            "takes a label or tool namespace, e.g. \"linkedin\" returns every LinkedIn "
            "connector, and an unknown name is refused with the closest names). ⚠️ `state` "
            "is only your TOOLBOX SELECTION: `not_selected` does NOT mean not connected. "
            "`credential` on a row = a key or account EXISTS for you (level, nature), even "
            "not_selected; it is never checked alive here — its `next_step` names the status "
            "tool (e.g. linkedin_unipile_account op=status): call it BEFORE concluding a "
            "connector is not connected. / select (install "
            "`name` — its tools do NOT mount in the current conversation: reach them right "
            "away via oto_call, or open a new one) / pause / unselect. Org admin, on "
            "`org_id`: op=force (install `name` in ONE `member`'s toolbox — refused, with the "
            "date, if they paused or removed it themselves; not an access grant) / recommend (set the org's KIT `connectors` as a whole list: the "
            "DIFFERENCE is installed for current members too, never over their own choice; [] "
            "empties it)."),
        mcp="oto_connector",
    ),
    Capability(
        key="connectors.console.instance", handler=_instance, Input=InstanceInput,
        authz=BY_OP({"list": SUB_ONLY, "lend": SUB_ONLY, "verify": ORG_MEMBER}),
        description=(
            "Connector INSTANCES (connector x auth/config; the secret is never returned). "
            "op=list (instances visible to you by proximity — member/group/org/tenant/platform, "
            "optional filters `connector`, `level`; `id` = `inst:<n>`, the instance's stable "
            "identifier — may be missing, and `ref` stays the pin handle for instance=; "
            "`visible_to` = the scopes that can DISCOVER it, derived from the access chain — "
            "descriptive, it does not filter this list) "
            "/ lend (lend YOUR instance of `connector` to a peer — `to` = the email or the "
            "sub of a member of your organization, who pins it — or to a team of your org — "
            "`to_group` = its id, whose members then resolve it by account name like a team "
            "key; revoke=true takes it back — ADR 0044 "
            "share_side) / verify (side-effect-free credential probe of "
            "`connector` → {ok, error}; level=auto tests the credential that resolves for "
            "you, level=org the org shared key). Contrast with oto_identity (operable "
            "accounts of ONE connector) and oto_connector op=list (catalog of TYPES)."),
        mcp="oto_instance",
    ),
    Capability(
        key="connectors.console.identity", handler=_identity, Input=IdentityInput,
        authz=SUB_ONLY,
        description=(
            "Connected identities/accounts your credential can act as for a connector — the "
            "Slack workspaces you posted tokens for, the LinkedIn accounts under your shared "
            "Unipile key, your Google accounts. op=list → each operable account with "
            "`is_default`, plus `granted:true`+`owner` when a peer shared THEIRS with you "
            "(#55), `shared:true`+`shared_scope` (team|org) for an account your team or org "
            "shares. **To act as one for a SINGLE call, pass `_account=<id>` on that tool** "
            "(e.g. slack_post_message(_account='client-x', …)) — an EPHEMERAL pin: it's how "
            "you use a granted account, or post to the other workspace, without changing your "
            "default, and it needs NO reconnection or key setup. ⚠️ The parameter is "
            "`_account`, WITH the underscore: the bare name belongs to the tools that have a "
            "business `account` argument of their own. op=set (`identity_id` from op=list) "
            "sets your PERSISTENT default identity instead (rejects an id your credential "
            "can't reach). With a single account posted, nothing to do — it resolves alone; "
            "with several and no default, a call without `_account` is REFUSED rather than "
            "sent under the wrong identity. op=rename (`identity_id` + `new_name`) renames a "
            "named account; `_account` then takes the new name. `scope=org` / `scope=group` "
            "reads or acts on the accounts shared by your org / active team (e.g. one key "
            "per company of a group) — set and rename there need that level's admin."),
        mcp="oto_identity",
    ),
    Capability(
        key="connectors.console.account_access", handler=_account_access, Input=AccountAccessInput,
        authz=SUB_ONLY,
        description=(
            "Shared connector accounts (#55) — who may OPERATE your connected messaging "
            "accounts, acting as you. op=list (grants you gave + grants you received) / "
            "grant (`channel` linkedin|whatsapp|…, `grantee`=email or sub — even outside "
            "your orgs; owner-only, audited) / revoke (immediate effect). Deny-by-default: "
            "no grant = nobody but the owner."),
        mcp="oto_account_access",
    ),
]
