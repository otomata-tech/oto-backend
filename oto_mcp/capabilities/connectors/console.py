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
ADR 0038: list/verify, and the account rules of #1499), `oto_identity` (identity selector
ADR 0024).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import (access_level as connectors_access_level,
               activation as connectors_activation,
               force as connectors_force,
               identities as connectors_identities,
               instances as connectors_instances,
               selection as connectors_selection,
               sharing as connectors_sharing,
               transfert as connectors_transfert,
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


# ── oto_connector_access_level : get / set / clear · scope org|group ─────────
class AccessLevelInput(BaseModel):
    op: Literal["get", "set", "clear"]
    scope: Literal["org", "group"] = "org"
    name: str                          # connector
    org_id: Optional[int] = None       # scope=org
    group_id: Optional[int] = None     # scope=group
    level: Optional[str] = None        # set


def _access_level(ctx: ResolvedCtx, inp: AccessLevelInput) -> dict:
    a = connectors_access_level
    if inp.op == "set" and inp.level is None:
        raise AuthzDenied(400, "missing_level", "`level` is required for set.")
    if inp.scope == "org":
        oid = _need(inp.org_id, "missing_org", "`org_id` is required for scope=org.")
        if inp.op == "get":
            return a._org_get(ctx, a.OrgAccessLevelInput(org_id=oid, name=inp.name))
        if inp.op == "set":
            return a._org_set(ctx, a.OrgAccessLevelSetInput(
                org_id=oid, name=inp.name, level=inp.level))
        return a._org_clear(ctx, a.OrgAccessLevelInput(org_id=oid, name=inp.name))
    gid = _need(inp.group_id, "missing_group", "`group_id` is required for scope=group.")
    if inp.op == "get":
        return a._group_get(ctx, a.GroupAccessLevelInput(group_id=gid, name=inp.name))
    if inp.op == "set":
        return a._group_set(ctx, a.GroupAccessLevelSetInput(
            group_id=gid, name=inp.name, level=inp.level))
    return a._group_clear(ctx, a.GroupAccessLevelInput(group_id=gid, name=inp.name))


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


# ── oto_instance : list / verify / rules (ADR 0038 §B, account rules #1499) ──
_RULE_OPS = ("rules", "set_rule", "remove_rule", "share_events", "transfer")
_RULE_LEVELS = ("none", "read", "write", "sensitive", "manage")


class InstanceInput(BaseModel):
    op: Literal["list", "verify", "rules", "set_rule", "remove_rule", "share_events",
                "transfer"]
    connector: Optional[str] = None
    # list: filter member|group|org|tenant|platform · verify: auto (effective credential) |
    # org · set_rule: the level the rule gives (none|read|write|sensitive|manage)
    level: Optional[str] = None
    instance: Optional[str] = None             # rule ops: the account, `inst:<n>`
    # rule ops: a member's email or sub, or the oto email of a person outside the org;
    # transfer: a member of the account's org
    to: Optional[str] = None
    to_group: Optional[int] = None             # rule ops: a team id (instead of `to`)
    to_org: bool = False                       # rule ops: the whole org (instead of `to`)
    services: Optional[list[str]] = None       # set_rule: Google/Microsoft — services covered
    limit: int = 100                           # share_events


def _instance_id(inp: InstanceInput) -> int:
    ref = _need(inp.instance, "missing_instance",
                f"`instance` (the account's `inst:<n>`, from op=list) is required for "
                f"{inp.op}.").strip()
    n = ref[len("inst:"):] if ref.startswith("inst:") else ref
    if not n.isdigit():
        raise AuthzDenied(400, "invalid_instance",
                          f"`instance` is an account's `inst:<n>` (from op=list), not `{ref}`.")
    return int(n)


def _rule_op(ctx: ResolvedCtx, inp: InstanceInput) -> dict:
    iid = _instance_id(inp)
    if inp.op == "rules":
        return connectors_sharing._list_rules(ctx, connectors_sharing.AccountInput(
            instance_id=iid))
    if inp.op == "share_events":
        return connectors_sharing._share_events(ctx, connectors_sharing.ShareEventsInput(
            instance_id=iid, limit=inp.limit))
    sujet = {"instance_id": iid, "to": inp.to, "to_group": inp.to_group, "to_org": inp.to_org}
    if inp.op == "transfer":
        return connectors_transfert._transfer(ctx, connectors_transfert.TransferInput(**sujet))
    if inp.op == "remove_rule":
        return connectors_sharing._remove_rule(ctx, connectors_sharing.RuleSubjectInput(**sujet))
    if inp.level not in _RULE_LEVELS:
        raise AuthzDenied(400, "invalid_level",
                          "op=set_rule: `level` ∈ none|read|write|sensitive|manage.")
    return connectors_sharing._set_rule(ctx, connectors_sharing.SetRuleInput(
        **sujet, level=inp.level, services=inp.services))


async def _instance(ctx: ResolvedCtx, inp: InstanceInput) -> dict:
    if inp.op == "list":
        if inp.level not in (None, "member", "group", "org", "tenant", "platform"):
            raise AuthzDenied(400, "invalid_level",
                              "op=list: `level` ∈ member|group|org|tenant|platform.")
        return connectors_instances._list_instances(
            ctx, connectors_instances.ListInstancesInput(connector=inp.connector, level=inp.level))
    if inp.op in _RULE_OPS:
        return _rule_op(ctx, inp)
    connector = _need(inp.connector, "missing_connector", f"`connector` is required for {inp.op}.")
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
        key="connectors.console.access_level", handler=_access_level,
        Input=AccessLevelInput, Output=connectors_access_level.AccessLevelState,
        authz=BY_OP({
            ("get", "org"): ORG_ADMIN_OF("org_id"),
            ("set", "org"): ORG_ADMIN_OF("org_id"),
            ("clear", "org"): ORG_ADMIN_OF("org_id"),
            ("get", "group"): GROUP_ADMIN_OF("group_id"),
            ("set", "group"): GROUP_ADMIN_OF("group_id"),
            ("clear", "group"): GROUP_ADMIN_OF("group_id"),
        }, fields=("op", "scope")),
        refresh_visibility=True,
        description=(
            "Access level of an OAuth connector that declares levels (e.g. zoho, zohodesk: "
            "`read`, `full`). op=get (the level set at this scope, the one in force, the "
            "declared levels and their scopes) / set (`name`, `level`) / clear (back to the "
            "level above). scope=org (`org_id`, org admin) | group (`group_id`, team lead; "
            "restrict-only, a level wider than the org's is refused). Under a level other "
            "than `full`, the tools and ops it does not cover are hidden and refused "
            "(`access_level_denied`), and the consent asks only for its scopes; widening "
            "needs a new consent."),
        mcp="oto_connector_access_level",
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
        authz=BY_OP({"list": SUB_ONLY, "verify": ORG_MEMBER, "rules": SUB_ONLY,
                     "set_rule": SUB_ONLY, "remove_rule": SUB_ONLY, "share_events": SUB_ONLY,
                     "transfer": SUB_ONLY}),
        description=(
            "Connector INSTANCES (connector x auth/config; the secret is never returned). "
            "op=list (instances visible to you by proximity — member/group/org/tenant/platform, "
            "optional filters `connector` — a service card such as `google_ads` finds the account it borrows — and `level`; `id` = `inst:<n>`, the instance's stable "
            "identifier — may be missing, and `ref` stays the pin handle for instance=) "
            "/ verify (side-effect-free credential probe of "
            "`connector` → {ok, error}; level=auto tests the credential that resolves for "
            "you, level=org the org shared key) "
            "/ the RULES of an account you manage (`instance` = its `inst:<n>`): each rule "
            "gives ONE subject — `to` (a member's email or sub, or the oto email of a person "
            "OUTSIDE the org), `to_group` (a team id of its org) or "
            "`to_org=true` — a level none < read < write < sensitive < manage; another "
            "person's account then serves that subject when the call names it "
            "(`_account=`), and unnamed when nothing of hers, her team's or her org's "
            "serves (the most recent loan first). op=rules (the rules, who manages the account, your level) / "
            "set_rule (`level`; `services` limits a Google or Microsoft account to some of "
            "its authorized services; only an org admin grants `manage`, a team lead at "
            "most `write`; never the owner) / remove_rule / share_events (the journal of "
            "the rules, `limit`) / transfer (give the account to a new owner — `to`, "
            "`to_group` or `to_org=true` of its org; towards a team or the org you must "
            "still manage it afterwards, only an org admin gives it to a person; it keeps "
            "its id and secret). An account of another org opened to you is listed with "
            "`lent_by_org` and `your_level`; it serves unnamed when no account of your "
            "org does (after an account of your org lent to you, and your own key "
            "elsewhere), and named always. "
            "Contrast with oto_identity (operable "
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
            "`is_default`; an account that is not yours (a peer's, your team's, your org's) "
            "carries `level` (read|write|sensitive|manage), the level its rules give you, and "
            "`owner`. **To act as one for a SINGLE call, pass `_account=<id>` on that tool** "
            "(e.g. slack_post_message(_account='client-x', …)) — an EPHEMERAL pin: another "
            "person's account serves named this way, at that level (unnamed only when "
            "nothing of yours, your team's or your org's serves — the response then says "
            "`lent_by`); it is also how you "
            "post to the other workspace without changing your "
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
]
