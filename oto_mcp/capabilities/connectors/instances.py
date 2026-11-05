"""Capabilities "connector instances" (ADR 0038 §B, rung 4) — a READ PROJECTION of
the existing vault (degenerate case): each credential the resolution cascade can
find (member (sub, org) > groups > org > granted/open platform key) is exposed as a
**named owned instance**. Metadata only — the secret is NEITHER decrypted NOR
returned (non-decrypting readers only: `list_credentials`, grants,
`list_platform_keys_meta`). Zero new tables, zero write path,
`resolve_credential`/`status_for` untouched.

LOT L6 (blueprint ADR 0053-D9, R1 settled on 27/08): each projected instance now
carries, IN ADDITION to its `ref`, the STABLE identifier from the
`connector_instances` table (`id = "inst:{n}"`). The projection remains what it was — a
vault read, zero decryption, zero writes: the only addition is **one** query
that translates the vault quadruplets into identifiers, fail-open. Nothing else reads
these instances yet (neither the cascade nor the resolution); `ref` remains the
reference to pass back (the pin guards refuse `inst:` by name, see `access.rbac`).
PIECE 2 (28/08): the instance is now born at SET time, in the vault transaction —
this projection did not change by a single byte, but its `id` is no longer missing
because of key freshness, only because of fail-open.

Another person's account is NOT listed: since the account rules (#1499) it serves only
when the call names it, at the level its rules give (`access/comptes`); its rules are
read by `oto_instance op=rules` (`connectors.instances.rules.list`).

EXCLUSIONS (deliberate, documented):
- `entity_type='user'` residues (legacy scope of the former atlassian/folkmcp
  federations, removed on 2026-09-09 — the rows themselves still sleep in the database)
  — outside the working cascade by design (ADR 0033);
- remote Unipile identities (`connector_identities`: enumerating them decrypts
  the key and calls the remote API) — the BYO key itself IS listed as a
  member instance.

LIMITS (documented):
- the `config_fields` packed INSIDE `secret_enc` (e.g. zoho `data_center` set via
  POST api-keys) do NOT come out (reading them = `unpack_secret` = decryption);
  only the (public) `meta` part is projected as `config`. The real B5 table
  will unpack at write time.
- no activation/exposure filter (ADR 0031): resolution doesn't apply one
  either — the instance exists even if the connector is not exposed
  (deliberate divergence from `oto_connector op=list`). Along the way the projection lists
  what `status_for` ignores (org grant, free-tier): it is the honest mirror of
  what resolution would find.
- NO `wins`/`mode` (the winner is still told by `status_for` — a single
  truth): the §C preference is carried by the SORT member < group < org <
  platform. B6 will make this order literal.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# NON-decrypting readers only — never get_credential*, never
# list_platform_keys (which decrypts), never the impoverished forms
# list_org_secrets/list_group_secrets (they overwrite account/meta/secret_kind).
from ... import access, credentials_store, db, group_store, instance_refs, providers
from . import custom_api, instances_tenant
from ._level_doc import DOC_LEVEL as _DOC_LEVEL, DOC_OWNER_TYPE as _DOC_OWNER_TYPE
from .._authz import SUB_ONLY
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding)
from ..registry import CAPABILITIES

logger = logging.getLogger(__name__)

# Proximity rank (§C): the cascade reread as levels, carried by the sort.
_LEVEL_RANK = {"member": 0, "group": 1, "org": 2, "tenant": 3, "platform": 4}


class ListInstancesInput(BaseModel):
    connector: Optional[str] = None      # filter by connector type
    level: Optional[Literal["member", "group", "org", "tenant", "platform"]] = (
        Field(default=None, description=_DOC_LEVEL))


class InstanceOwner(BaseModel):
    """Owner of an instance. `type='user'` carries a sub, `group`/`org` an
    integer, `platform` **no id** (a platform key is identified by its
    label, ADR 0044 §F) — hence three optional fields rather than a fixed pair."""
    type: Literal["user", "group", "org", "tenant", "platform"] = Field(
        description=_DOC_OWNER_TYPE)
    # sub (user) or group/org id (an INTEGER), or the tenant slug. Absent on platform.
    id: Optional[Union[int, str]] = None
    label: Optional[str] = None             # group name, or platform key label


class ConnectorInstance(BaseModel):
    """An instance = a connector × an auth setup, projected from the
    vault (read-only). Metadata only: the secret is NEITHER decrypted
    NOR returned.

    Two families behind the same shape, and their keys differ: a VAULT
    instance (member/group/org) carries `account`/`secret_kind`/`config`/`set_by`;
    a PLATFORM instance has no vault row, hence none of that —
    only `daily_quota` (grant) or `set_at` (free-tier)."""
    model_config = ConfigDict(extra="allow")

    # The STABLE IDENTIFIER of the instance (`inst:{id}`, lot L6) — the TARGET shape,
    # served IN ADDITION to `ref` and meant to replace it (it and the shell's
    # `connectionId`). What `ref` cannot do: survive the renaming of an account
    # or of a platform key label (the composed ref projects the vault key, which
    # MOVES the row on rename), designate a sub-instance, designate an
    # instance without a secret.
    # ⚠️ **May be absent**, and the client must cope — but no longer for yesterday's
    # reason: since piece 2 (28/08) the instance is born at SET time, in the vault
    # transaction, so a fresh key has its identifier. The only remaining absence is
    # the fail-open of `_stamp_instance_identity` (the query did not answer). Until the
    # switch is made, `ref` remains the reference to pass back (`_instance=`,
    # bindings) — `id` is there to be stored and prepared, not yet to be pinned
    # (the pin guards refuse it by name).
    # Opaque like `ref`: pass it back as-is, never parse it.
    id: Optional[str] = None
    # Opaque and STABLE handle, target of an `_instance=` pin. Do not parse it.
    ref: str
    connector: str
    # PROXIMITY rank in the cascade, which carries the sort (member < group < org <
    # platform). It is NOT the winner: the list never says who resolves —
    # a single truth for that, `status_for`.
    level: Literal["member", "group", "org", "tenant", "platform"] = Field(
        description=_DOC_LEVEL)
    owner: InstanceOwner
    # DERIVED, never stored: `meta.label` > "Connector · account" > "Connector ·
    # key label" > connector label. Two instances may therefore bear the
    # same name; `ref` is the identity.
    name: str
    # Discriminates several instances of the same connector at the same level (multi-account).
    # `""` = the default instance. Absent from platform instances.
    account: Optional[str] = None
    secret_kind: Optional[str] = None
    # PUBLIC part of the meta only. ⚠️ The `config_fields` packed INSIDE the secret
    # (e.g. zoho `data_center`) are NOT in it — reading them would mean decrypting.
    # An empty `config` therefore does not mean "no configuration".
    config: Optional[dict] = None
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    # WHERE the instance comes from. CLOSED set, and the five values are hard-coded in this
    # module — none is derived from data, so the contract can name them:
    #
    # | `credential`         | a vault row within my reach (member, team, org) |
    # | `tenant_key`         | the caller's TENANT key, between the org and the platform |
    # | `user_grant`         | platform level granted to ME |
    # | `org_grant`          | platform level granted to my ORG |
    # | `free_tier`          | open platform key, no grant (ADR 0031) |
    #
    # ⚠️ Deliberate gap with `AuthDescriptor.method`, which stays a `str` for fear
    # that an enum would break a generated client the day one more value is added. Two
    # reasons to decide otherwise here: `method` is DERIVED (a function computes it
    # per connector), `via` is a literal set in five places of a single module; and
    # `level`, its immediate neighbour in this same model, is already a `Literal`. The
    # ratchet `tests/test_instance_via_declare.py` reads the module's AST and goes red at the
    # sixth value — it is THAT which makes the enum tenable, not discipline.
    via: Literal["credential", "tenant_key", "user_grant", "org_grant", "free_tier"] = Field(
        description=(
            "Where the instance comes from. `credential`: a vault row within my reach "
            "(member, team, org). `tenant_key`: the caller's tenant key. "
            "`user_grant` / `org_grant` / `free_tier`: platform levels (granted to "
            "me, to my org, or open without a grant)."))
    is_default: Optional[bool] = None       # present (true) only if marked default
    # Present (true) only if the instance is set aside: the cascade SKIPS it,
    # but it stays listed and reactivable — a `suspended` is not an absence.
    suspended: Optional[bool] = None
    daily_quota: Optional[int] = None       # granted platform levels only
    # `http` instances only: the custom connector's card (`custom_api.py`). May be absent.
    custom_api: Optional[dict] = None
    # An account of ANOTHER org (D1, #1499) — it serves only NAMED (`_account=`), and of
    # that org the listing says its name, nothing else (no owner, no author):
    # `lent_by_org` = a rule of that org opens it to you; `elsewhere_org` = your own
    # personal account there (#172).
    lent_by_org: Optional[str] = None
    elsewhere_org: Optional[str] = None
    # Accounts of another org only: the level you hold on it (none < read < write <
    # sensitive < manage). `level`, above, stays the proximity rank.
    your_level: Optional[Literal["none", "read", "write", "sensitive", "manage"]] = None


class ConnectorInstances(BaseModel):
    """The instances visible from the active org, by proximity.

    Two deliberate blind spots, which mean an absence proves nothing: the list
    applies NO activation/exposure filter (an instance exists even if
    the connector is not exposed — deliberate divergence from `connectors.me`), and
    the stable identifiers are logged fail-open (an incident leaves `id` absent)."""
    instances: list[ConnectorInstance]
    count: int                              # = len(instances), AFTER filters


class SuspendInstanceResult(BaseModel):
    """Echo of setting aside (or reactivating) MY member key."""
    connector: str
    # `null` when the targeted instance is the one without a named account (the input's
    # `""` comes out as `null`) — not "unknown account".
    account: Optional[str] = None
    suspended: bool


def _connector_label(connector: str) -> str:
    c = providers.REGISTRY.get(connector)
    return c.label if (c is not None and c.label) else connector


def _instance_name(connector: str, meta_label, account: str = "",
                   key_label: str = "") -> str:
    """DERIVED, deterministic instance name (nothing stored): non-empty `meta.label`
    > "Connector · account" > "Connector · key label" (platform)
    > connector label."""
    clabel = _connector_label(connector)
    if meta_label:
        return str(meta_label)
    if account:
        return f"{clabel} · {account}"
    if key_label:
        return f"{clabel} · {key_label}"
    return clabel


def _vault_key(entity_type: str, entity_id, connector: str, account: str) -> tuple:
    """The four-column key of a vault row — the LINK to its instance
    (`connector_instances`), which carries the same quadruplet. Vault convention:
    `account` is `''` in single-account mode, never None."""
    return (entity_type, str(entity_id), connector, account or "")


def _cred_instance(level: str, owner: dict, ref: str, row: dict,
                   vault_key: Optional[tuple] = None) -> dict:
    """Projects a vault row (`list_credentials` shape) into an instance.
    `meta` already comes out filtered `_public_meta` at the source; we re-apply
    `public_meta` as defense in depth (never a bearer to the client).
    `config` = public meta MINUS the keys extracted at top level."""
    meta = credentials_store.public_meta(row.get("meta"))
    account = row.get("account") or ""
    inst = {
        "ref": ref,
        "connector": row["connector"],
        "level": level,
        "owner": owner,
        "name": _instance_name(row["connector"], meta.get("label"), account),
        "account": account,
        "secret_kind": row.get("secret_kind"),
        "config": {k: v for k, v in meta.items() if k not in ("label", "is_default", "suspended")},
        "set_by": row.get("set_by"),
        "set_at": row.get("set_at"),
        "via": "credential",
        # PRIVATE key, removed before serialization (`_stamp_instance_identity`): it only
        # serves to resolve the stable identifier in ONE query for the whole list.
        "_vault_key": vault_key,
    }
    if meta.get("is_default"):
        inst["is_default"] = True
    # SUSPENDED state (lot 2): instance set aside → skipped by the cascade, but
    # listed for the KeyStack ("suspended · Reactivate").
    if meta.get("suspended"):
        inst["suspended"] = True
    return inst


def _platform_instance(provider: str, label: str, via: str, extra: dict) -> dict:
    """"Platform key" instance (ADR 0044 §F: identified by (connector, label),
    no more platform_key_id surrogate)."""
    return {
        "ref": instance_refs.make_platform_ref(provider, label),
        "connector": provider,
        "level": "platform",
        "owner": {"type": "platform", "label": label},
        "name": _instance_name(provider, None, key_label=label),
        # ADR 0044 §F: a platform key IS a vault row — `entity_type`
        # 'platform', `entity_id` = its label. It therefore has an instance like the
        # others, including when reached through a grant or the free-tier.
        "_vault_key": _vault_key(credentials_store.PLATFORM, label, provider, ""),
        **extra,
        # ⚠️ AFTER `extra`, and that is the point: `via` is declared in the contract as a
        # CLOSED set, so no auxiliary dict must be able to slip an eighth
        # value into it. Before `extra`, a caller passing `{"via": …}` would produce
        # a value outside the enum — a generated client fails on that, on data
        # that nothing would have flagged.
        "via": via,
    }


def _platform_eligible(provider: str) -> bool:
    """The cascade's platform path is gated on `auth_modes` (see
    `resolve_credential`): a byo-only provider NEVER resolves a platform
    key — projecting one would be a lie (review B4, major finding)."""
    con = providers.REGISTRY.get(provider)
    return con is not None and "platform" in con.auth_modes


def _stamp_instance_identity(out: list[dict]) -> None:
    """Sets `id = inst:{id}` on each instance, and REMOVES the private key.

    **One query for the whole list**, never one per instance: the projection runs
    inline on a single-loop server, against a remote managed database — a lookup per
    instance would make N round trips on a surface that returns dozens.

    **Logged fail-open**: `ref` is still served. Taking down a user's key listing
    because the instances table did not answer would be out of proportion. The
    counterpart is explicit in the output model: `id` **may be absent**.

    ⚠️ The private key `_vault_key` is removed in ALL cases (the output model
    is `extra="allow"`: whatever stays in the dict goes out on the wire).
    """
    cles = [i.get("_vault_key") for i in out]
    for i in out:
        i.pop("_vault_key", None)
    # An account of another org carries its identifier already (`_comptes_d_ailleurs`).
    cles = [k if "id" not in i else None for i, k in zip(out, cles)]
    vraies = [k for k in cles if k]
    try:
        connues = db.instances_for_vault_rows(vraies)
    except Exception:
        logger.warning("instances: stable identifiers unavailable (fail-open)",
                       exc_info=True)
        return
    for inst, cle in zip(out, cles):
        ligne = connues.get(cle) if cle else None
        if ligne is not None:
            inst["id"] = instance_refs.make_instance_ref(ligne["id"])
    custom_api.stamp_instances(out, connues, cles)


_PROXIMITE = {"member": ("member", "user"), "group": ("group", "group"),
              "org": ("org", "org")}


def _comptes_d_ailleurs(sub: str, org: int, connector: Optional[str]) -> list[dict]:
    """The accounts of OTHER orgs than `org` that `sub` can name (`access.comptes_hors_org`),
    as instances. Their `ref` is their stable identifier: the composed ref would carry the
    owner's sub, and nothing of that org is said but its name."""
    appelant = access.Appelant(sub, org, lambda: access.current_group(sub))
    carrier = providers.credential_provider(connector) if connector else None
    out = []
    for compte in access.comptes_hors_org(carrier, org, sub):
        level, owner_type = _PROXIMITE[compte.owner_type]
        ident = instance_refs.make_instance_ref(compte.instance_id)
        nom_d_org = compte.org_nom or f"#{compte.org_id}"
        inst = {"id": ident, "ref": ident, "connector": compte.connector, "level": level,
                "owner": {"type": owner_type},
                "name": _instance_name(compte.connector, compte.label, compte.account),
                "account": compte.account, "via": "credential",
                "your_level": access.niveau(appelant, compte)}
        if compte.proprietaire == sub:
            inst["elsewhere_org"] = nom_d_org
            inst["owner"] = {"type": "user", "id": sub}
        else:
            inst["lent_by_org"] = nom_d_org
        out.append(inst)
    return out


def _list_instances(ctx: ResolvedCtx, inp: ListInstancesInput) -> dict:
    # SYNC handler run INLINE by the capability adapters (pattern of the existing
    # capabilities — no threadpool here): short indexed queries.
    sub, org = ctx.sub, ctx.org_id
    out: list[dict] = []

    if org is not None:
        # 1. MEMBER — my credentials in THIS org (ADR 0033: never an org-agnostic
        # fallback). One (connector, account) row = one instance.
        member_eid = credentials_store.member_id(org, sub)
        for row in credentials_store.list_credentials(credentials_store.MEMBER,
                                                      member_eid):
            out.append(_cred_instance(
                "member", {"type": "user", "id": sub},
                instance_refs.make_member_ref(org, sub, row["connector"],
                                              row.get("account") or ""),
                row,
                _vault_key(credentials_store.MEMBER, member_eid, row["connector"],
                           row.get("account") or "")))

        # 2. GROUPS — the groups I can READ (mirror of `can_read_group`,
        # the resolution guard): my groups, and ALL the org's groups for
        # an org_admin (roles.py escalation — "one connector per department, seen at
        # org level": the admin sees and administers every departmental instance).
        # Since B3, `_group=` is a call token → every instance listed here is
        # reachable = visible in the §C sense.
        from ... import roles as _roles
        try:
            org_admin = _roles.is_org_admin(sub, org)
        # noqa: SILENT — declared debt: partial enumeration returned as complete (#424, verdict C)
        except Exception:
            org_admin = False
        groups = (group_store.list_groups(org) if org_admin
                  else group_store.list_groups_for_user(sub, org))
        for g in groups:
            gid = g.get("group_id") or g.get("id")
            owner = {"type": "group", "id": gid, "label": g.get("name")}
            for row in credentials_store.list_credentials("group", str(gid)):
                out.append(_cred_instance(
                    "group", owner,
                    instance_refs.make_group_ref(gid, row["connector"],
                                                 row.get("account") or ""),
                    row,
                    _vault_key("group", gid, row["connector"],
                               row.get("account") or "")))

        # 3. ORG — secrets of the active org, visible to every member (precedent:
        # the org sheet already lists them to members).
        for row in credentials_store.list_credentials("org", str(org)):
            out.append(_cred_instance(
                "org", {"type": "org", "id": org},
                instance_refs.make_org_ref(org, row["connector"],
                                           row.get("account") or ""),
                row,
                _vault_key("org", org, row["connector"],
                           row.get("account") or "")))

    # 3 ter. OTHER ORGS (D1, #1499) — an account another org opens to me by a rule, and
    # my own personal key in another org (#172): listed so that I can NAME it, with the
    # name of its org and my level, nothing else of that org.
    if org is not None:
        out.extend(_comptes_d_ailleurs(sub, org, inp.connector))

    # 3 bis. TENANT (L-keys PR 2) — the key of the CALLER's tenant (qualified sub),
    # between the org and the platform as in the walker. Empty for a bare account.
    for slug, ref, row in instances_tenant.tenant_rows(sub):
        inst = _cred_instance("tenant", {"type": "tenant", "id": slug}, ref, row,
                              _vault_key(credentials_store.TENANT, slug, row["connector"],
                                         row.get("account") or ""))
        inst["via"] = "tenant_key"
        out.append(inst)

    # 4. PLATFORM — user grants + org grants + free-tier. The cascade resolves only ONE
    # platform key per provider (user_grant > org_grant > free_tier) → dedup
    # by PROVIDER (insertion order = priority; dedup by key listed a phantom
    # free-tier after rotation, review B4). `auth_modes` gate mirrors
    # resolution: a byo-only provider projects no platform level.
    seen_providers: set = set()
    for gr in db.list_grants_for_user(sub):
        if gr["provider"] in seen_providers or not _platform_eligible(gr["provider"]):
            continue
        seen_providers.add(gr["provider"])
        out.append(_platform_instance(
            gr["provider"], gr.get("label") or "", "user_grant",
            {"daily_quota": gr.get("daily_quota")}))
    if org is not None:
        for gr in db.list_org_grants(org):
            if (gr["provider"] in seen_providers
                    or not _platform_eligible(gr["provider"])):
                continue
            seen_providers.add(gr["provider"])
            out.append(_platform_instance(
                gr["provider"], gr.get("label") or "", "org_grant",
                {"daily_quota": gr.get("daily_quota")}))
    # Free-tier ADR 0031: most recent key of each `platform_key_open` provider,
    # usable without a grant (ADR 0044 §F: PLATFORM-scope instances of the unified vault,
    # sorted set_at DESC → first encountered per provider = the most recent).
    last_open: dict[str, dict] = {}
    for k in credentials_store.list_platform_credentials():
        con = providers.REGISTRY.get(k["provider"])
        if con is not None and con.platform_key_open and k["provider"] not in last_open:
            last_open[k["provider"]] = k
    for k in last_open.values():
        if k["provider"] in seen_providers or not _platform_eligible(k["provider"]):
            continue
        seen_providers.add(k["provider"])
        out.append(_platform_instance(
            k["provider"], k.get("label") or "", "free_tier",
            {"set_at": k.get("set_at")}))

    # Input filters. A service card (`google_ads`, `gmail`…) owns no vault row: it is
    # filtered under its carrier (`credential_provider`).
    if inp.connector:
        carrier = providers.credential_provider(inp.connector)
        out = [i for i in out if providers.credential_provider(i["connector"]) == carrier]
    if inp.level:
        out = [i for i in out if i["level"] == inp.level]

    # §C preference carried by the SORT: member < group < org < platform.
    out.sort(key=lambda i: (i["connector"], _LEVEL_RANK[i["level"]],
                            i.get("account") or ""))
    # The stable identifier, LAST: after the
    # filters, to resolve only what comes out, and because the private key must
    # disappear from EVERYTHING that comes out.
    _stamp_instance_identity(out)
    return {"instances": out, "count": len(out)}


CAPABILITIES += [
    Capability(
        key="connectors.instances.list",
        handler=_list_instances,
        Input=ListInstancesInput,
        Output=ConnectorInstances,
        authz=SUB_ONLY,   # org_id injected from the actor seam, never from a client param
        description=(
            "List the connector INSTANCES (connector x auth/config) visible to you in the active "
            "org, by proximity: yours (member), your groups', the org's, your tenant's "
            "(accounts of a third-party tenant only), then platform grants. "
            "Metadata only — the secret is never returned. `id` (`inst:<n>`) is the stable "
            "identifier of the instance and will replace `ref`; it is set as soon as the key is "
            "stored, but may still be missing if it could not be read, so keep using `ref` as "
            "the pin handle for now. Both are opaque: pass them back as-is. Another "
            "person's account of your org is not listed: name it (`_account=`) to use it, "
            "at the level its rules give you. An account of ANOTHER org opened to you by a "
            "rule is listed with `lent_by_org` (that org's name) and `your_level`, your own "
            "key in another org with `elsewhere_org`: both serve when named, and unnamed "
            "when no account of your org does. Contrast with "
            "oto_identity (operable accounts of ONE connector) and oto_connector op=list "
            "(catalog of TYPES)."),
        rest=RestBinding("GET", "/api/me/connector-instances"),
    ),
]


class SuspendInstanceInput(BaseModel):
    connector: str                       # connector type (e.g. "unipile")
    account: str = ""                    # discriminates if several member instances
    suspended: bool = True               # False = reactivate


def _suspend_instance(ctx: ResolvedCtx, inp: SuspendInstanceInput) -> dict:
    """Suspends / reactivates YOUR member key for the connector in the active org (lot 2).
    A suspended instance is SKIPPED by credential resolution (the rung
    below — group/org/platform — takes over), but stays listed and
    reactivable (`suspended=False`). Writes ONLY `meta.suspended` — the secret is
    never touched nor read. Reserved to YOUR own key (SUB_ONLY, actor-seam org)."""
    sub, org = ctx.sub, ctx.org_id
    if org is None:
        raise AuthzDenied(400, "no_active_org",
                          "No active org to resolve the instance.")
    ok = credentials_store.update_meta(
        credentials_store.MEMBER, credentials_store.member_id(org, sub),
        inp.connector, inp.account, {"suspended": bool(inp.suspended)})
    if not ok:
        raise AuthzDenied(404, "no_instance",
                          f"No member key '{inp.connector}' to suspend.")
    return {"connector": inp.connector, "account": inp.account or None,
            "suspended": bool(inp.suspended)}


CAPABILITIES += [
    Capability(
        key="connectors.instances.suspend",
        handler=_suspend_instance,
        Input=SuspendInstanceInput,
        Output=SuspendInstanceResult,
        authz=SUB_ONLY,   # your own key, actor-seam org — never a third party
        description=(
            "Suspend or reactivate YOUR OWN member key for a connector in the active "
            "org. A suspended instance is SKIPPED by credential resolution (the next "
            "level down — group/org/platform — takes over) but stays listed and "
            "reactivable (`suspended=false`). Only meta.suspended is written; the "
            "secret is never touched."),
        errors=(DeclaredError(400, "no_active_org",
                              "no context org: an instance is set aside "
                              "WITHIN a workspace"),
                DeclaredError(404, "no_instance",
                              "no key of yours for this connector and account "
                              "— there is nothing to suspend"),),
        rest=RestBinding("POST", "/api/me/connector-instances/suspend"),
    ),
]
