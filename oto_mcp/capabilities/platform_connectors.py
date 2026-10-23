"""PLATFORM tier of connectors: the activation switch, and the access the platform
opens to an org or to a member.

Five hand-written routes until 2026-08-27, ported to capabilities (ADR 0009) —
same paths, same codes, same body on the wire:

- `GET|POST|DELETE /api/admin/connectors/activation`            → the activation switch (ADR 0010 B4)
- `GET|POST        /api/admin/connectors/{provider}/platform-access` → platform access (ADR 0044 §H)

This is the missing level of a family whose TWO other tiers were already
capabilities: `connectors.activation.{org_list,set_org,clear_org}` and
`{group_list,set_group,clear_group}`. Same domain, three levels, one single way to
describe it from now on.

**Both the global master and the org override take effect immediately**, with no restart:
`register_all` loads the tools of ALL registry connectors at boot, and
activation is read from the database on every request (`connectors.activation.exposed_connectors`,
by session visibility and by the call guards — ADR 0011, 16/06/2026). Until 23/09/2026 the
response carried a `restart_required: true` on the global master:
a leftover from before ADR 0011, which would have made us redeploy production for an effect
already obtained (oto-backend#815, measured on 02/09: public catalogue going from 97 to 98 connectors
without a restart). Removed, not set to `false`: an always-false field says nothing.

**No MCP face** (`mcp=None`): flipping the global master affects the whole
platform, and opening platform access is a commercial act. An agent has nothing to do with it, and the tiers a user can
actually drive — org and team — are already served by `oto_connector_activation`.

`/api/admin/*` is removed from the public OpenAPI description: a platform console has
no third-party integrator. The `Output` is used here for type generation on the dashboard side and
for the readability of the contract, not for publication.
"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, StrictBool, StrictInt

from .. import access, db, entitlements_catalogue as catalogue, org_store, providers
from . import _facturation_externe as facturation_externe
from ..connectors import activation as connector_activation
from ._authz import PLATFORM_ADMIN, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ACTIVATION = "/api/admin/connectors/activation"
_ACCESS = "/api/admin/connectors/{provider}/platform-access"


# --- Inputs -----------------------------------------------------------------

class ActivationListInput(BaseModel):
    """No parameters: the admin sees the WHOLE registry, including what is OFF —
    that is their surface for turning it on."""


class ActivationSetInput(BaseModel):
    connector: str = ""
    # ⚠️ `Strict*` + default `None`: the route returns `400 enabled_must_be_bool` when the
    # field is MISSING, not pydantic's `400 invalid_input`. The `None` default therefore
    # lets the missing case — the only one that occurs — fall into the handler, with its
    # historical code. A field that is present but WRONGLY TYPED is still refused by pydantic (same 400,
    # code `invalid_input`): nobody sends one, and the schema stays accurate.
    enabled: Optional[StrictBool] = None
    # Absent ⇒ GLOBAL master. Present ⇒ override for THIS org.
    org_id: Optional[StrictInt] = None


class ActivationClearInput(BaseModel):
    # Query string: both arrive as TEXT. We keep them as is and convert
    # in the handler, so as to return `connector_and_org_id_required` and `org_id_must_be_int`
    # — the two codes served — rather than a generic `invalid_input`.
    connector: Optional[str] = None
    org_id: Optional[str] = None


class PlatformAccessInput(BaseModel):
    provider: str


class PlatformAccessSetInput(BaseModel):
    provider: str
    scope: Optional[str] = None            # 'org' | 'user'
    # An org id arrives as a number, a sub as text: both are accepted and
    # normalized to text, as `str(body.get("id", ""))` used to do.
    id: Union[int, str, None] = None
    on: bool = False


# --- Outputs ----------------------------------------------------------------

class ActivationOverride(BaseModel):
    org_id: int
    enabled: bool


class ActivationRow(BaseModel):
    """⚠️ **`enabled: null` is not "unknown", it is OFF.** The master has never been
    set, and the rule is deny-by-default: the connector is not exposed. A front end that
    treats `null` as "enabled" or as an indeterminate state is wrong."""
    connector: str
    label: Optional[str] = None
    help: Optional[str] = None
    namespaces: list[str]
    enabled: Optional[bool] = None
    # The orgs that deviate from the global master, in either direction.
    overrides: list[ActivationOverride]
    # Paid option that gates this connector (layer 3, ADR 0044 §H), or `null`.
    paid_option: Optional[str] = None


class ActivationListView(BaseModel):
    connectors: list[ActivationRow]


class ActivationSetView(BaseModel):
    """The switch as set, served from the next request (global master and org
    override alike: activation is read from the database on every request)."""
    ok: bool
    connector: str
    enabled: bool
    org_id: Optional[int] = None


class ActivationClearView(BaseModel):
    """The override removed: the connector falls back to the global master for this org."""
    ok: bool
    connector: str
    org_id: int


class Beneficiary(BaseModel):
    """An org or a member to whom the platform opens this connector. `has_key` = grant
    on the platform key (layer 2); `has_option` = option offered (layer 3). The
    two are independent: one without the other is a normal state, not an inconsistency.

    `daily_quota` = the daily quota of the key grant, read from `meta.rate_limit_by`
    of the platform instance whose `share_down` names this beneficiary; `null` = no
    quota set, or no key grant. ⚠️ `platform_revoke` ERASES this quota: anyone who
    wants to re-grant identically must note it BEFORE revoking. For a connector
    moved to the chain model (`grants_chain.CHAIN_CONNECTORS`), the grant is a live
    EDGE and its quota the edge's constraint: both are reported here too — see
    `PlatformAccessView.chain_grants`."""
    # Seuls `user` et `org` passent les trois gardes de `touch` (#437).
    scope: Literal["org", "user"]
    id: str
    has_key: bool
    has_option: bool
    daily_quota: Optional[int] = None  # quota of the key grant (meta.rate_limit_by)
    label: Optional[str] = None
    logo_url: Optional[str] = None     # orgs
    email: Optional[str] = None        # membres


class PlatformAccessView(BaseModel):
    """Connector-centric view of platform access (ADR 0044 §H) — it replaces the
    scattered levers `/platform/orgs` and `/platform/users`. **No secret leaves it.**

    ⚠️ `open_tier: true` changes how `beneficiaries` is read: a platform instance
    with `open` sharing opens the connector to EVERYONE without a named grant, so the list
    is no longer the population served — it only lists the explicit grants.

    `chain_grants: true` = this connector's grants are EDGES (`grants_chain`), and
    `beneficiaries` includes every org or member holding a LIVE one. A reader that
    decides on this list (who is served by a CLOSED key, who still has to be granted
    or cut) can then trust it for a chain connector as for any other. Absent or
    false on a chain connector means a backend that does not list them: there the
    list is blind to those grants, and must not be read as « serves nobody »."""
    connector: str
    paid_option: Optional[str] = None   # None = no paid option (layer 3)
    platform_key: bool                  # a platform key exists (layer 2)
    open_tier: bool                     # free-tier: open to everyone without a grant
    chain_grants: bool = False          # chain connector: live edges are listed
    beneficiaries: list[Beneficiary]


class PlatformAccessSetView(BaseModel):
    """The SINGLE "platform access" act: it sets TOGETHER what the backend already coupled
    — the comp option (if the connector has one) AND the platform key grant (if
    one exists). `paid_option`/`platform_key` say what was actually touched."""
    ok: bool
    connector: str
    # Écho de l'entrée, refusée (400) hors de ces deux valeurs (#437).
    scope: Literal["org", "user"]
    id: str
    on: bool
    paid_option: Optional[str] = None
    platform_key: bool


# --- Handlers ---------------------------------------------------------------

def _known(connector: str, *, code: str, status: int):
    if connector not in providers.REGISTRY:
        raise AuthzDenied(status, code)
    return providers.REGISTRY[connector]


def _list_activation(ctx: ResolvedCtx, inp: ActivationListInput) -> dict:
    """The whole registry × its activation state (global + org overrides)."""
    glob: dict[str, bool] = {}
    overrides: dict[str, list] = {}
    for r in connector_activation.list_activations():
        if r["org_id"] is None:
            glob[r["connector"]] = bool(r["enabled"])
        else:
            overrides.setdefault(r["connector"], []).append(
                {"org_id": r["org_id"], "enabled": bool(r["enabled"])}
            )
    out = [
        {
            "connector": name,
            "label": c.label,
            "help": c.help,
            "namespaces": list(c.namespaces),
            "enabled": glob.get(name),  # None = never set = OFF
            "overrides": overrides.get(name, []),
            "paid_option": access.paid_option_for(name),  # layer 3 (ADR 0044 §H) or None
        }
        for name, c in providers.REGISTRY.items()
    ]
    return {"connectors": out}


def _set_activation(ctx: ResolvedCtx, inp: ActivationSetInput) -> dict:
    """Set activation: global master if `org_id` is absent, otherwise org override."""
    if inp.connector not in providers.REGISTRY:
        raise AuthzDenied(400, "unknown_connector")
    if not isinstance(inp.enabled, bool):
        raise AuthzDenied(400, "enabled_must_be_bool")
    connector_activation.set_activation(inp.connector, inp.enabled, org_id=inp.org_id,
                                        set_by=ctx.sub)
    return {
        "ok": True,
        "connector": inp.connector,
        "enabled": inp.enabled,
        "org_id": inp.org_id,
    }


def _clear_override(ctx: ResolvedCtx, inp: ActivationClearInput) -> dict:
    """Delete an org override (the connector falls back to the global master)."""
    if not inp.connector or not inp.org_id:
        raise AuthzDenied(400, "connector_and_org_id_required")
    try:
        org_id = int(inp.org_id)
    except ValueError:
        raise AuthzDenied(400, "org_id_must_be_int")
    connector_activation.clear_activation(inp.connector, org_id)
    return {"ok": True, "connector": inp.connector, "org_id": org_id}


def _platform_access(ctx: ResolvedCtx, inp: PlatformAccessInput) -> dict:
    """[platform_admin] The orgs and members to whom the platform opens this connector:
    grantees of the platform key (`share_down` of PLATFORM-scope instances, ADR 0044
    §F) ∪ beneficiaries of the comp option (layer 3). No secret."""
    _known(inp.provider, code="unknown_connector", status=404)
    from .. import credentials_store, grants_chain
    from ..db import grants as db_grants
    option = access.paid_option_for(inp.provider)

    acc: dict[str, dict] = {}

    def touch(scope: str, sid: str) -> dict:
        k = f"{scope}:{sid}"
        if k not in acc:
            acc[k] = {"scope": scope, "id": sid, "has_key": False, "has_option": False,
                      "daily_quota": None}
        return acc[k]

    insts = credentials_store.list_platform_instances(inp.provider)
    open_tier = any(i["share_mode"] == "open" for i in insts)
    for inst in insts:
        # A grant's quota lives next to the grant, on the SAME instance
        # (`credentials_store.platform_grant`). Read-only: nothing about resolution
        # changes, and a chain connector keeps the quota on its edge.
        quotas = (inst.get("meta") or {}).get("rate_limit_by") or {}
        for g in inst["share_down"]:
            scope, _, sid = str(g).partition(":")
            if scope in ("user", "org") and sid:
                rec = touch(scope, sid)
                rec["has_key"] = True
                if rec["daily_quota"] is None:
                    rec["daily_quota"] = quotas.get(str(g))
    # A chain connector's grant is an EDGE, never a `share_down` entry
    # (`credentials_store.platform_grant`). Read from `share_down` alone, a closed
    # key of such a connector listed NOBODY whoever had been granted: the console
    # said « serves no workspace » after a grant that had worked, and a billing
    # service that diffs on this list could neither converge nor cut (seen on
    # 2026-10-12, a chain key re-posted closed). Live edges only — a revoked one
    # grants nothing — and only named grantees: the « everyone » edge is `open_tier`'s.
    chain = grants_chain.is_chained(inp.provider)
    if chain:
        for inst in insts:
            if not inst.get("label"):
                continue
            ref = grants_chain.instance_ref(inst["label"], inp.provider)
            for edge in db_grants.live_edges_for_resource(ref):
                if edge.get("grantee_kind") in ("user", "org") and edge.get("grantee_id"):
                    rec = touch(edge["grantee_kind"], str(edge["grantee_id"]))
                    rec["has_key"] = True
                    if rec["daily_quota"] is None:
                        rec["daily_quota"] = grants_chain.quota_of(edge)
    if option:
        for c in db.list_option_comps_for_option(option):
            if c["entity_type"] in ("user", "org"):
                touch(c["entity_type"], str(c["entity_id"]))["has_option"] = True

    out = []
    for rec in acc.values():
        if rec["scope"] == "org":
            o = org_store.get_org(int(rec["id"])) if rec["id"].isdigit() else None
            rec["label"] = o["name"] if o else f"org #{rec['id']}"
            rec["logo_url"] = org_store.effective_logo_url(o) if o else None
        else:
            u = db.get_user(rec["id"])
            rec["label"] = (u.get("name") or u.get("email") or rec["id"]) if u else rec["id"]
            rec["email"] = u.get("email") if u else None
        out.append(rec)
    out.sort(key=lambda r: (r["scope"], (r["label"] or "").lower()))
    return {
        "connector": inp.provider,
        "paid_option": option,          # None = no paid option (layer 3)
        "platform_key": bool(insts),    # a platform key exists (layer 2)
        "open_tier": open_tier,         # free-tier: open to everyone without a grant
        "chain_grants": chain,          # chain connector: live edges are listed above
        "beneficiaries": out,
    }


def _set_platform_access(ctx: ResolvedCtx, inp: PlatformAccessSetInput) -> dict:
    """[super_admin] SINGLE "platform access" act (ADR 0044 §H): opens/closes
    an org's or a member's access to a connector = sets TOGETHER the comp option
    (layer 3) AND the platform key grant (layer 2) — what the backend
    already coupled, exposed in one gesture."""
    _known(inp.provider, code="unknown_connector", status=404)
    sid = str(inp.id if inp.id is not None else "").strip()
    if inp.scope not in ("org", "user") or not sid:
        raise AuthzDenied(400, "invalid_body")
    # existence (no grant to a ghost)
    if inp.scope == "org":
        if not sid.isdigit() or not org_store.get_org(int(sid)):
            raise AuthzDenied(404, "unknown_org")
    elif not db.get_user(sid):
        raise AuthzDenied(404, "unknown_user")

    from .. import credentials_store
    option = access.paid_option_for(inp.provider)
    if option and catalogue.est_du_catalogue(option):
        # This connector's option is a catalogue entitlement, which oto-commerce sets alone
        # (#1097). The composite gesture can no longer set half of it: the key is shared
        # on its own, via the grant for the targeted scope — the org or the account.
        partage = (" To share only the platform key with this account: "
                   "`platform.key.grant` (POST /api/admin/users/{sub}/grants/{provider})."
                   if inp.scope == "user" else
                   " To share only the platform key with this org: "
                   "`platform.org.grant_key` (POST /api/admin/orgs/{id}/grants/{provider}).")
        raise facturation_externe.refus(
            f"Opening platform access to {inp.provider!r} (the {option!r} option)", partage)
    has_key = bool(credentials_store.list_platform_instances(inp.provider))
    if not option and not has_key:
        # neither paid option nor platform key → nothing to open on the platform side
        raise AuthzDenied(400, "no_platform_access")
    gscope = f"{inp.scope}:{sid}"
    on = bool(inp.on)
    if on:
        if option:
            db.set_option_comp(inp.scope, sid, option, granted_by=ctx.sub)
        if has_key:
            credentials_store.platform_grant(inp.provider, gscope)
    else:
        if option:
            db.clear_option_comp(inp.scope, sid, option)
        if has_key:
            credentials_store.platform_revoke(inp.provider, gscope)
    return {
        "ok": True, "connector": inp.provider, "scope": inp.scope, "id": sid, "on": on,
        "paid_option": option, "platform_key": has_key,
    }


_DOC_LIST = (
    "The whole connector registry × its activation state: global master and "
    "per-org overrides. ⚠️ `enabled: null` means OFF (never set, deny-by-default), "
    "not \"indeterminate\"."
)
_DOC_SET = (
    "Sets a connector's activation: GLOBAL master if `org_id` is absent, override "
    "for THIS org otherwise. Either takes effect from the next request, with no "
    "restart: activation is read from the database on every request."
)
_DOC_CLEAR = (
    "Removes an org override: the connector falls back to the global master. Both "
    "parameters are required."
)
_DOC_ACCESS = (
    "The orgs and members to whom the PLATFORM opens this connector — platform key "
    "grants and option-gift marks (`has_option`), merged into one "
    "connector-centric view. No secret. ⚠️ `has_option` is the LEGACY mark of a gift "
    "(`option_comps`), not the entitlement: the entitlement of a paid option is set by the "
    "billing service (oto-commerce) and is read from the declared entitlements. ⚠️ If "
    "`open_tier` is true, the connector is open to everyone without a grant: the list then "
    "no longer shows the population served. `chain_grants: true` = a chain connector, "
    "whose grants are edges: every org or member holding a LIVE one is listed (`has_key`)."
)
_DOC_SET_ACCESS = (
    "Opens or closes an org's or a member's platform access to a connector, in one "
    "gesture: sets the option mark and the platform key grant together, depending on what "
    "the connector has. Refuses a grant to a non-existent org or account, and a "
    "connector that has neither option nor platform key (`no_platform_access`). ⚠️ Refuses with "
    "409 `billing_moved` a connector whose option is a catalogue entitlement "
    "(`unipile`): that entitlement is set by the billing service (oto-commerce) alone; "
    "the platform key is then shared via `platform.org.grant_key` (an org) or "
    "`platform.key.grant` (an account)."
)

CAPABILITIES += [
    Capability(
        key="platform.connector.activation_list", handler=_list_activation,
        Input=ActivationListInput, authz=PLATFORM_ADMIN, Output=ActivationListView,
        description=_DOC_LIST,
        mcp=None,   # deployment act: the drivable tiers are org/team
        rest=RestBinding("GET", _ACTIVATION),
    ),
    Capability(
        key="platform.connector.activation_set", handler=_set_activation,
        Input=ActivationSetInput, authz=PLATFORM_ADMIN, Output=ActivationSetView,
        description=_DOC_SET,
        mcp=None,
        rest=RestBinding("POST", _ACTIVATION),
    ),
    Capability(
        key="platform.connector.activation_clear", handler=_clear_override,
        Input=ActivationClearInput, authz=PLATFORM_ADMIN, Output=ActivationClearView,
        description=_DOC_CLEAR,
        mcp=None,
        rest=RestBinding("DELETE", _ACTIVATION),
    ),
    Capability(
        key="platform.connector.access_list", handler=_platform_access,
        Input=PlatformAccessInput, authz=PLATFORM_ADMIN, Output=PlatformAccessView,
        description=_DOC_ACCESS,
        mcp=None,
        rest=RestBinding("GET", _ACCESS),
    ),
    Capability(
        key="platform.connector.access_set", handler=_set_platform_access,
        Input=PlatformAccessSetInput, authz=SUPER_ADMIN, Output=PlatformAccessSetView,
        description=_DOC_SET_ACCESS,
        errors=(facturation_externe.declaration(
            "The connector's option is a key of the entitlements catalogue, which only the "
            "billing service (oto-commerce) sets; the platform key is shared "
            "via `platform.org.grant_key` (an org) or `platform.key.grant` (an account)."),),
        mcp=None,
        rest=RestBinding("POST", _ACCESS),
    ),
]


# ── OVERRIDABLE connector properties (L6 piece 2 c2) ─────────────────────────
#
# "The database may take precedence over the code default" (Alexis, 27/08), so that widening a
# connector does not require a deployment. A single property today — auth
# CARDINALITY (`mono`|`multi`).
#
# ⚠️ **`reload` is not an implementation detail, it is half of the gesture.** The
# overrides live in MEMORY (cardinality is consulted up to 4× per tool
# call, on a single-loop server: one query per lookup would freeze the
# loop). Setting a row therefore changes nothing until we reload — and the
# reload is **PER PROCESS**, exactly like the issuer registry:
# reloading preprod does not reload prod.

_SETTINGS = "/api/admin/connectors/settings"


class ConnectorSettingInput(BaseModel):
    op: str = "list"                         # list | set | clear | reload
    connector: Optional[str] = None          # set / clear
    key: str = "cardinality"                 # cardinality, or a connector setting
                                             # (e.g. instagram_meta.app_id/app_secret)
    value: Optional[str] = None              # set: 'mono' | 'multi', or the setting's value
    org_id: Optional[int] = None             # None = PLATFORM override


class ConnectorSettingView(BaseModel):
    """What the console renders. `active` is the snapshot of what the PROCESS is applying
    right now — not what the database contains: that is precisely the gap an admin
    must be able to see before wondering why their setting "doesn't work"."""
    op: str
    rows: list[dict] = []                    # the database rows
    active: dict = {}                        # the LIVE overrides of this process
    loaded: Optional[int] = None             # reload: how many were installed
    changed: Optional[bool] = None           # set / clear


#: Suffix of the keys whose VALUE is not read back. The table first held
#: settings that are public by design (the three coordinates of the Planity application,
#: which every browser receives); `instagram_meta.app_secret` on 2026-09-09 is the
#: first REAL secret stored there, and `op="list"` until then returned every value
#: as is — to a caller who is often an AGENT, hence into a transcript.
#:
#: Redaction by SUFFIX, not by key list: a list indexed by name would have to
#: be kept up to date with each new connector, and forgetting would fail nowhere — it
#: would publish the secret. A trailing `_secret` is a convention we can require of
#: the author of a setting, and that a test verifies.
_SUFFIXE_SECRET = "_secret"


def _sans_les_secrets(lignes: list[dict]) -> list[dict]:
    """The rows, with the values of secret keys replaced by a marker.

    We return the PRESENCE, never the value — even truncated: knowing that a key is
    set is what the admin needs to diagnose; reading it back brings them
    nothing they did not already have when they set it."""
    out = []
    for r in lignes:
        ligne = dict(r)
        if str(ligne.get("key", "")).endswith(_SUFFIXE_SECRET):
            ligne["value"] = "(set — value not shown)" if ligne.get("value") else ""
        out.append(ligne)
    return out


def _connector_setting(ctx: ResolvedCtx, inp: ConnectorSettingInput) -> dict:
    from ..connectors import cardinality
    from ..db import connector_settings as store

    def _actives() -> dict:
        return {f"{s}:{i}:{c}": v
                for (s, i, c), v in cardinality.overrides_snapshot().items()}

    if inp.op == "list":
        return {"op": "list",
                "rows": _sans_les_secrets(store.list_connector_settings(inp.key)),
                "active": _actives()}
    if inp.op == "reload":
        # No fallback: if the read fails, the caller must KNOW — a reload
        # that returned "ok" on an unreachable database is the worst of both worlds.
        n = cardinality.reload()
        return {"op": "reload", "loaded": n, "active": _actives()}

    if not inp.connector:
        raise AuthzDenied(400, "missing_connector", "`connector` required for set/clear.")
    if providers.REGISTRY.get(inp.connector) is None:
        raise AuthzDenied(404, "unknown_connector",
                          f"Unknown connector: {inp.connector!r}.")
    scope_type, scope_id = (("org", str(inp.org_id)) if inp.org_id is not None
                            else ("platform", "platform"))
    if inp.op == "clear":
        ok = store.clear_connector_setting(scope_type, scope_id, inp.connector, inp.key)
        return {"op": "clear", "changed": ok, "active": _actives()}
    if inp.op != "set":
        raise AuthzDenied(400, "unsupported_op", f"Unknown op: {inp.op!r}.")
    if inp.key == cardinality.KEY and inp.value not in (cardinality.MONO,
                                                        cardinality.MULTI):
        # NAMED refusal rather than a row that loading will silently ignore: an
        # override believed to be set that nobody reads is the defect this batch
        # exists to close.
        raise AuthzDenied(400, "invalid_cardinality",
                          f"`value` must be {cardinality.MONO!r} or "
                          f"{cardinality.MULTI!r} (got {inp.value!r}).")
    from ._cle_exigee import CLE_REGLAGE, FAUX, VRAI
    if inp.key == CLE_REGLAGE:
        # Same NAMED refusal as cardinality, for the same reason, and more serious
        # here: `True` or `oui` would be read as FALSE at reservation time, and an org believed
        # to be constrained would keep running on the platform's key.
        if inp.value not in (VRAI, FAUX):
            raise AuthzDenied(400, "invalid_setting",
                              f"`{CLE_REGLAGE}` is {VRAI!r} or {FAUX!r} "
                              f"(got {inp.value!r}).")
        from .. import providers as _p
        if getattr(_p.REGISTRY.get(inp.connector), "kind", None) != "credential":
            raise AuthzDenied(400, "invalid_setting",
                              f"`{CLE_REGLAGE}` can only be set on a MODEL "
                              f"provider (connector of type credential) — "
                              f"`{inp.connector}` is not one.")
    from ..tools import forager as _forager
    if inp.connector == "forager" and inp.key == _forager.PLATFORM_OPS_KEY:
        # An instance choice — what OUR key serves —, never an org's; and a list the
        # tool would not understand is refused here, not read as "less" at call time.
        if inp.org_id is not None:
            raise AuthzDenied(400, "invalid_setting",
                              f"`{_forager.PLATFORM_OPS_KEY}` is set platform-wide only "
                              "(omit `org_id`).")
        try:
            _forager.parse_platform_ops(str(inp.value))
        except ValueError as e:
            raise AuthzDenied(400, "invalid_setting",
                              f"`{_forager.PLATFORM_OPS_KEY}`: {e}.")
    store.set_connector_setting(scope_type, scope_id, inp.connector, inp.key,
                                str(inp.value), set_by=ctx.sub)
    return {"op": "set", "changed": True, "active": _actives()}


CAPABILITIES += [
    Capability(
        key="platform.connector.setting", handler=_connector_setting,
        Input=ConnectorSettingInput, authz=SUPER_ADMIN, Output=ConnectorSettingView,
        description=(
            "Connector properties that the DATABASE may override, per platform or per "
            "org — the registry constant is only the default. op=list / set "
            "(`connector`, `key`, `value`; `org_id` omitted = platform-wide) / clear / "
            "reload. Keys: `cardinality` (`mono`|`multi`): how many "
            "accounts one entity may hold for that connector; `platform.ops` on "
            "`forager`, platform-wide: what the shared key serves, comma-separated "
            "`tool:op` or `tool` (default `forager_person:phone_numbers`, read at each "
            "call). ⚠️ Overrides are held in "
            "MEMORY, so a row takes effect only after `reload` — and reload is "
            "PER-PROCESS: reloading preprod does not reload prod."),
        mcp="oto_admin_connector_setting",
        rest=RestBinding("POST", _SETTINGS),
    ),
]
