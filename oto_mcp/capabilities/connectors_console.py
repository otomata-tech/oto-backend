"""Console connecteurs MCP consolidée (ADR 0047, B1) — fusion `*_op`.

Réunit les 26 tools MCP de la famille connecteurs en 6, un par objet métier,
verbe en param `op` (+ `scope` org|équipe quand le grain existe aux deux
niveaux) — le pattern de la console admin (`admin_console.py`) appliqué à la
surface non-admin. L'autz reste DÉCLARÉE (combinateur `BY_OP`, clé `(op, scope)`
quand le palier dépend des deux) ; les handlers de domaine sont réutilisés tels
quels (on construit leur Input spécifique) ; les faces REST des capacités
d'origine ne bougent pas — seul leur binding `mcp=` est retiré.

Concepts : `oto_connector_activation` (exposition org/équipe),
`oto_connector_access` (RBAC ADR 0025, org/équipe), `oto_connector`
(marketplace + actes d'org : force/recommend), `oto_instance` (instances
ADR 0038/0044 : list/lend/verify), `oto_identity` (sélecteur d'identité
ADR 0024), `oto_account_access` (comptes partagés #55).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from . import (
    connectors_account_grants,
    connectors_acl,
    connectors_activation,
    connectors_force,
    connectors_identities,
    connectors_instances,
    connectors_selection,
    connectors_sharing,
    connectors_verify,
)
from ._authz import (
    BY_OP,
    GROUP_ADMIN_OF,
    GROUP_MEMBER_OF,
    ORG_ADMIN_OF,
    ORG_MEMBER,
    ORG_MEMBER_OF,
    SUB_ONLY,
)
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


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
    name: Optional[str] = None         # set/clear : connecteur
    enabled: Optional[bool] = None     # set


def _activation(ctx: ResolvedCtx, inp: ActivationInput) -> dict:
    a = connectors_activation
    if inp.scope == "org":
        oid = _need(inp.org_id, "missing_org", "`org_id` requis pour scope=org.")
        if inp.op == "list":
            return a._org_list(ctx, a.OrgActivationListInput(org_id=oid))
        name = _need(inp.name, "missing_name", f"`name` (connecteur) requis pour {inp.op}.")
        if inp.op == "set":
            if inp.enabled is None:
                raise AuthzDenied(400, "missing_enabled", "`enabled` requis pour set.")
            return a._org_set(ctx, a.OrgActivationSetInput(org_id=oid, name=name, enabled=inp.enabled))
        return a._org_clear(ctx, a.OrgActivationClearInput(org_id=oid, name=name))
    gid = _need(inp.group_id, "missing_group", "`group_id` requis pour scope=group.")
    if inp.op == "list":
        return a._group_list(ctx, a.GroupActivationListInput(group_id=gid))
    name = _need(inp.name, "missing_name", f"`name` (connecteur) requis pour {inp.op}.")
    if inp.op == "set":
        if inp.enabled is None:
            raise AuthzDenied(400, "missing_enabled", "`enabled` requis pour set.")
        return a._group_set(ctx, a.GroupActivationSetInput(group_id=gid, name=name, enabled=inp.enabled))
    return a._group_clear(ctx, a.GroupActivationClearInput(group_id=gid, name=name))


# ── oto_connector_access : list / grant / revoke · scope org|group (ADR 0025) ─
class AccessInput(BaseModel):
    op: Literal["list", "grant", "revoke"]
    scope: Literal["org", "group"] = "org"
    org_id: Optional[int] = None
    group_id: Optional[int] = None
    connector: Optional[str] = None
    principal_type: Optional[Literal["group", "user"]] = None  # scope=org
    principal_id: Optional[str] = None                          # scope=org
    member: Optional[str] = None                                # scope=group : sub


def _access(ctx: ResolvedCtx, inp: AccessInput) -> dict:
    acl = connectors_acl
    if inp.scope == "org":
        oid = _need(inp.org_id, "missing_org", "`org_id` requis pour scope=org.")
        if inp.op == "list":
            return acl._list_acl(ctx, acl.AclListInput(org_id=oid))
        set_inp = acl.AclSetInput(
            org_id=oid,
            connector=_need(inp.connector, "missing_connector", "`connector` requis."),
            principal_type=_need(inp.principal_type, "missing_principal",
                                 "`principal_type` (group|user) requis pour scope=org."),
            principal_id=_need(inp.principal_id, "missing_principal",
                               "`principal_id` requis pour scope=org."))
        return acl._grant(ctx, set_inp) if inp.op == "grant" else acl._revoke(ctx, set_inp)
    gid = _need(inp.group_id, "missing_group", "`group_id` requis pour scope=group.")
    if inp.op == "list":
        return acl._group_list_acl(ctx, acl.GroupAclListInput(group_id=gid))
    set_inp = acl.GroupAclSetInput(
        group_id=gid,
        connector=_need(inp.connector, "missing_connector", "`connector` requis."),
        member=_need(inp.member, "missing_member", "`member` (sub) requis pour scope=group."))
    return acl._group_grant(ctx, set_inp) if inp.op == "grant" else acl._group_revoke(ctx, set_inp)


# ── oto_connector : list / select / pause / unselect · force / recommend ─────
class ConnectorInput(BaseModel):
    op: Literal["list", "select", "pause", "unselect", "force", "recommend"]
    name: Optional[str] = None                 # select/pause/unselect/force
    verbose: bool = False                      # list
    state: Optional[str] = None                # list : not_selected|active|paused
    org_id: Optional[int] = None               # force/recommend
    member: Optional[str] = None               # force : sub ou email
    connectors: Optional[list[str]] = None     # recommend : baseline ([] efface)


async def _connector(ctx: ResolvedCtx, inp: ConnectorInput) -> dict:
    sel = connectors_selection
    if inp.op == "list":
        return sel._me(ctx, sel.MyConnectorsInput(verbose=inp.verbose, state=inp.state))
    if inp.op in ("select", "pause", "unselect"):
        action = sel.ConnectorActionInput(
            name=_need(inp.name, "missing_name", f"`name` (connecteur) requis pour {inp.op}."))
        return {"select": sel._select, "pause": sel._pause, "unselect": sel._unselect}[inp.op](ctx, action)
    oid = _need(inp.org_id, "missing_org", f"`org_id` requis pour {inp.op}.")
    if inp.op == "force":
        return await connectors_force._force_connector(ctx, connectors_force.ForceConnectorInput(
            org_id=oid,
            connector=_need(inp.name, "missing_name", "`name` (connecteur) requis pour force."),
            member=_need(inp.member, "missing_member", "`member` (sub ou email) requis pour force.")))
    if inp.connectors is None:
        raise AuthzDenied(400, "missing_connectors",
                          "`connectors` (liste de noms, [] pour effacer) requis pour recommend.")
    return sel._recommend(ctx, sel.RecommendInput(org_id=oid, connectors=inp.connectors))


# ── oto_instance : list / lend / verify (ADR 0038 §B, 0044 share_side) ───────
class InstanceInput(BaseModel):
    op: Literal["list", "lend", "verify"]
    connector: Optional[str] = None
    # list : filtre member|group|org|platform · verify : auto (credential effectif) | org
    level: Optional[str] = None
    to: Optional[str] = None                   # lend : sub du pair
    account: str = ""                          # lend
    revoke: bool = False                       # lend : True = reprendre le prêt


async def _instance(ctx: ResolvedCtx, inp: InstanceInput) -> dict:
    if inp.op == "list":
        if inp.level not in (None, "member", "group", "org", "platform"):
            raise AuthzDenied(400, "invalid_level",
                              "op=list : `level` ∈ member|group|org|platform.")
        return connectors_instances._list_instances(
            ctx, connectors_instances.ListInstancesInput(connector=inp.connector, level=inp.level))
    connector = _need(inp.connector, "missing_connector", f"`connector` requis pour {inp.op}.")
    if inp.op == "lend":
        return connectors_sharing._lend_instance(ctx, connectors_sharing.LendInstanceInput(
            connector=connector,
            to=_need(inp.to, "missing_to", "`to` (sub du pair) requis pour lend."),
            account=inp.account, revoke=inp.revoke))
    if inp.level not in (None, "auto", "org"):
        raise AuthzDenied(400, "invalid_level", "op=verify : `level` ∈ auto|org.")
    return await connectors_verify._verify(
        ctx, connectors_verify.VerifyInput(provider=connector, level=inp.level or "auto"))


# ── oto_identity : list / set (sélecteur d'identité, ADR 0024) ───────────────
class IdentityInput(BaseModel):
    op: Literal["list", "set"]
    connector: str
    identity_id: Optional[str] = None          # set


async def _identity(ctx: ResolvedCtx, inp: IdentityInput) -> dict:
    ids = connectors_identities
    if inp.op == "list":
        return await ids._list(ctx, ids.IdentitiesInput(connector=inp.connector))
    return await ids._set_default(ctx, ids.SetIdentityInput(
        connector=inp.connector,
        identity_id=_need(inp.identity_id, "missing_identity", "`identity_id` requis pour set.")))


# ── oto_account_access : list / grant / revoke (comptes partagés, #55) ───────
class AccountAccessInput(BaseModel):
    op: Literal["list", "grant", "revoke"]
    channel: Optional[connectors_account_grants.Channel] = None
    grantee: Optional[str] = None              # sub ou email


def _account_access(ctx: ResolvedCtx, inp: AccountAccessInput) -> dict:
    ag = connectors_account_grants
    if inp.op == "list":
        return ag._list(ctx, ag.AccountGrantsListInput())
    grant_inp = ag.AccountGrantInput(
        channel=_need(inp.channel, "missing_channel", "`channel` requis."),
        grantee=_need(inp.grantee, "missing_grantee", "`grantee` (sub ou email) requis."))
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
        key="connectors.console.access", handler=_access, Input=AccessInput,
        authz=BY_OP({
            ("list", "org"): ORG_ADMIN_OF("org_id"),
            ("grant", "org"): ORG_ADMIN_OF("org_id"),
            ("revoke", "org"): ORG_ADMIN_OF("org_id"),
            ("list", "group"): GROUP_MEMBER_OF("group_id"),
            ("grant", "group"): GROUP_ADMIN_OF("group_id"),
            ("revoke", "group"): GROUP_ADMIN_OF("group_id"),
        }, fields=("op", "scope")),
        refresh_visibility=True,
        description=(
            "Connector access rules — internal RBAC (ADR 0025): reserve a connector to a "
            "subset; the first principal makes it restricted (deny-by-default), removing the "
            "last reopens it. op=list / grant / revoke. scope=org (`org_id`, org admin; "
            "principal_type=group|user + principal_id) | group (`group_id`, team lead; "
            "`member`=sub — narrows the org's rules, never expands them)."),
        mcp="oto_connector_access",
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
            "default, verbose=true for the full card, filter with `state`) / select (install "
            "`name` — its tools do NOT mount in the current conversation: reach them right "
            "away via oto_call, or open a new one) / pause / unselect. Org admin, on "
            "`org_id`: op=force (push `name` into a `member`'s toolbox — visibility only, "
            "not an access grant) / recommend (set the org baseline `connectors`, [] clears)."),
        mcp="oto_connector",
    ),
    Capability(
        key="connectors.console.instance", handler=_instance, Input=InstanceInput,
        authz=BY_OP({"list": SUB_ONLY, "lend": SUB_ONLY, "verify": ORG_MEMBER}),
        description=(
            "Connector INSTANCES (connector x auth/config; the secret is never returned). "
            "op=list (instances visible to you by proximity — member/group/org/platform, "
            "optional filters `connector`, `level`; `ref` = stable pin handle for instance=) "
            "/ lend (lend YOUR instance of `connector` to a peer `to`=sub, revoke=true takes "
            "it back — ADR 0044 share_side) / verify (side-effect-free credential probe of "
            "`connector` → {ok, error}; level=auto tests the credential that resolves for "
            "you, level=org the org shared key). Contrast with oto_identity (operable "
            "accounts of ONE connector) and oto_connector op=list (catalog of TYPES)."),
        mcp="oto_instance",
    ),
    Capability(
        key="connectors.console.identity", handler=_identity, Input=IdentityInput,
        authz=SUB_ONLY,
        description=(
            "Connected identities/accounts your credential can act as for a connector (e.g. "
            "the LinkedIn accounts under your shared Unipile key, or your Google accounts). "
            "op=list → each operable account with `is_default`, plus `granted:true`+`owner` "
            "when a peer shared THEIRS with you (#55). **To act as one for a SINGLE call, pass "
            "`account=<id>` on that tool** (e.g. unipile_search(account=<id>, …)) — an "
            "EPHEMERAL pin: it's how you use a granted account without changing your default, "
            "and needs NO reconnection or key setup. op=set (`identity_id` from op=list) sets "
            "your PERSISTENT default identity instead (rejects an id your credential can't reach)."),
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
