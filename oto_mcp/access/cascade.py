"""The single WALKER of the credentials cascade (ADR 0024/0044).

The cascade `personal > cross-org > active team > org > tenant > platform` was written by
hand in six places; it lives here, and nowhere else. The walker is
parameterized by a PROBE (`CascadeProbe`): presence (no decryption),
fetch (decrypts only the winner), or preloaded (the same answers in a
few reads). Adding a rung means editing `walk_cascade` — never a
caller.

The platform tier (membership of a sharing scope, quota, grants chain) lives in
`platform_grant.py`, re-exported here. This module knows neither quotas, nor
RBAC, nor the actual resolution: they are the ones that call it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import (providers, credentials_store, db, grants_chain, group_store, org_store,
                tenant_vault)
from ..connectors import cardinality
from . import heritage, platform_grant, secret_repr

# DERIVED from the single-source registry (`providers/` package): providers whose
# secret can be OWNED by an org and shared (auth_mode byo_org) — excludes
# slack (xoxp = personal identity) and per-user sessions (linkedin/google/
# whatsapp/crunchbase). Gates the walker's group/org rungs.
ORG_SHAREABLE_PROVIDERS = providers.ORG_SHAREABLE_PROVIDERS

# ⚠️ EMPTY since 2026-09-09 (ADR 0069); rung kept, rows are still dormant at scope.
LEGACY_USER_SCOPE_PROVIDERS: tuple[str, ...] = ()


def _is_multi_account(provider: str, org: "int | None" = None) -> bool:
    """Does the connector carry several accounts in the vault (`account` segment)?
    Gates the account-selection path; a single-account connector keeps the
    historical resolution (account='').

    ⚠️ **Goes through `connectors.cardinality`, never through the registry property**, and
    this is not a style indirection: an org may have OVERRIDDEN the cardinality
    in the DB (L6 piece 2 c2), and the override must be read here as it is by the
    write guard. Read on one side only, it would accept a second account that
    nobody would ever read — the exact defect of oto-backend#409. Zero queries:
    overrides live in memory, loaded at boot and reloaded by hand.

    `org` = the requester's CONTEXT org. None ⟹ only the platform override applies."""
    return cardinality.is_multi_account(provider, org)


def account_noun(provider: str) -> str:
    """The provider's WORD for an account of this connector — "workspace" for Slack,
    "organisation" for Zoho, "site" for the signed-in browser, "compte" by
    default (`Connector.account_noun`). Used in the messages the AGENT reads when it
    is blocked: "multiple slack accounts" forces it to translate, "multiple
    workspaces" tells it what to look for. Never empty."""
    con = providers.connector_for_provider(provider)
    # French on purpose: the dashboard displays and inflects it.
    return (getattr(con, "account_noun", "") or "compte") if con else "compte"


class CompteAmbigu(McpError):
    """Several accounts at the same tier, without a single default: the caller must name
    one. Typed so that a REST face renders it as a named refusal (`account_required`) instead
    of a bare 500 — recognized by its class, never by its text."""


class CompteIntrouvable(McpError):
    """The account NAMED by the call (param, `_account=`, project pin) exists at no
    reachable tier. Typed, carrying the name, so that a connector words the refusal in
    its own vocabulary (Google lists the connected addresses) without reading the text
    (oto-backend#1160) — never a fallback to another account."""

    def __init__(self, error: ErrorData, account: str):
        super().__init__(error)
        self.account = account


def _shared_auto_account(entity_type: str, entity_id: str, provider: str,
                         where: str, scope: Optional[str] = None) -> str:
    """AUTOMATIC account of a multi-account tier, when the caller named none:
    single account auto > set default (`oto_identity(op='set')`,
    meta.is_default) > '' if no account > ambiguity McpError. Module-level because
    shared between the actual resolution (`_pick_account`) and the ANONYMOUS resolution
    (`_resolve_credential_anon`) — the `<slug>.mcp.oto.cx` endpoint must select
    the org account like the org rung of the real path, never read `''` hard-coded
    (`ensure_named_coexistence` migrates the `''` row to "principal" at the first
    named account — review #399 F3). `scope` non-None ⇒ the `oto_identity` refs in the
    ambiguity message carry the tier's scope (org/group)."""
    # At the team tier, instances LENT to the team count as its accounts.
    accts = (group_store.list_group_accounts(int(entity_id), provider)
             if entity_type == "group" else
             credentials_store.list_accounts(entity_type, entity_id, provider))
    if len(accts) == 1:
        return accts[0]["account"]
    if not accts:
        return ""
    defaults = [a for a in accts if (a.get("meta") or {}).get("is_default")]
    if len(defaults) == 1:
        return defaults[0]["account"]
    sc = f", scope='{scope}'" if scope else ""
    noun = account_noun(provider)
    # Agreement-free phrasing: the account noun comes from the registry and may be
    # grammatically feminine in French — a fixed past-participle agreement was wrong every other time.
    noms = ", ".join(f"`{a['account']}`" for a in accts)
    raise CompteAmbigu(ErrorData(
        code=INVALID_PARAMS,
        message=(
            f"Multiple {noun}s `{provider}` {where} ({noms}), with no single default — "
            f"pass `_account=\"<name>\"` on this call, or set a default with "
            f"oto_identity(op='set'{sc}, connector='{provider}', identity_id='<name>')."
        )))


def personal_instance_org(sub: str, provider: str,
                          exclude_org: Optional[int] = None) -> Optional[int]:
    """Org carrying the cross-org PERSONAL instance of `sub` for a per-person
    connector (issue #172, track A; `Connector.personal_cross_org`), or None.

    Deterministic (never a silent choice between two identities of the SAME human): the
    MOST RECENTLY set key. The personal org no longer has a preference (2026-09-29: a
    personal org is an org like any other). `exclude_org` rules out the context org (already tested
    upstream by the local member tier). Safe by construction: same `sub` ⟹ zero
    impersonation — we only find THEIR own key set elsewhere.

    `provider` is normalized to the credential carrier (delegation): the personal
    key of a unipile channel is the ACCOUNT's, and the org to retain is the one where
    that key lives. The hosted account will be looked up in the SAME org
    (`connectors/identities._own_unipile_account_id`) — key and account paired,
    never the key from here with the account from there."""
    provider = providers.credential_provider(provider)
    orgs = [o for o in credentials_store.list_member_orgs_for(sub, provider)
            if o != exclude_org]
    return orgs[0] if orgs else None  # set_at DESC → the most recent


# ── Single cascade walker ──────────────────────────────────────────────────────
# The cascade `personal > cross-org > active team > org > tenant > platform` was written by
# hand in 6 places (resolution, mode, status ×2, anonymous, publication probe)
# — every new rung had to be carried over N times, and every omission made a
# surface LIE (seen 2026-07-16: status_for's fields loop stayed
# user-only; 2026-07-07: option rule copied 3×, diverged). Here: ONE walk,
# parameterized by the PROBE — `presence` (no decryption, batchable /api/me)
# or `fetch` (decrypts only the winner). Every cascade change is made here
# and nowhere else.

@dataclass(frozen=True)
class CascadeRung:
    """A WINNING rung of the walk: level + entity + probe payload
    (`payload` = secret/grant in fetch, True/meta in presence). `via` distinguishes the
    LOCAL member key (editable here) from the cross-org personal instance (#172)."""
    mode: str                       # user | group | org | tenant | platform
    entity_type: Optional[str]      # credentials_store.MEMBER | 'group' | 'org' | TENANT | PLATFORM
    entity_id: Optional[str]
    payload: object
    account: str = ""
    via: str = "local"              # local | cross_org | grant | heritage

    def __repr__(self) -> str:
        """Redacted UNCONDITIONALLY (#564): `payload` carries the decrypted secret in
        fetch mode, and nothing at runtime tells this mode apart from the
        PRESENCE probe, whose payload is harmless. See `secret_repr`."""
        return secret_repr.expurge(self, "payload")


@dataclass(frozen=True)
class CascadeProbe:
    """Probe of a rung — same interface for presence and fetch. `member` returns
    `(payload, account)` or None (the resolution fetch encapsulates its multi-account
    selection there, McpErrors included); `member_cross` is always single-account;
    `tenant` receives the SLUG (L-keys PR 1) and answers like `org`; `platform` returns
    the grant (meta or resolved) or None. REQUIRED field for each: a probe that
    forgot a rung would skip it silently — the defect of #409."""
    member: Callable[[str, int, str], Optional[tuple]]
    member_cross: Callable[[str, int, str], Optional[object]]
    legacy_user: Callable[[str, str], Optional[object]]
    group: Callable[[int, str], Optional[object]]
    org: Callable[[int, str], Optional[object]]
    tenant: Callable[[str, str], Optional[object]]
    platform: Callable[[Optional[str], str, Optional[int]], Optional[dict]]


# A SUSPENDED member instance (batch 2 / ADR 0044 §KeyStack) is folded into the
# real probes: the key exists in the vault but the cascade treats it as absent
# → resolution AND status skip the member rung (the level below takes
# over). It stays listed by `oto_instance op=list` (KeyStack), reactivatable.
# ⚠️ The member probe of the RESOLUTION path is NOT this one: it is `_member_fetch`
# (in `_resolve_credential_impl`), which carries the multi-account selection — it must
# return the SAME suspension verdict (seen #401: it did not read it, a suspended
# key still won while KeyStack announced the takeover).
PRESENCE_PROBE = CascadeProbe(
    member=lambda s, o, p: ((True, "") if db.has_member_api_key(s, o, p)
                            and not db.member_instance_suspended(s, o, p) else None),
    member_cross=lambda s, o, p: (True if db.has_member_api_key(s, o, p) else None),
    legacy_user=lambda s, p: (True if credentials_store.has_credential(
        credentials_store.USER, s, p) else None),
    group=lambda g, p: (True if group_store.has_group_secret(g, p) else None),
    org=lambda o, p: (True if org_store.has_org_secret(o, p) else None),
    tenant=lambda t, p: (True if tenant_vault.has_tenant_secret(t, p) else None),
    platform=lambda s, p, o: platform_grant._platform_grant_meta(s, p, o),
)

# ⚠️ The org/group probes of FETCH_PROBE read the MONO account (`account=''`): the
# paths that must see the NAMED accounts of a shared tier (actual resolution
# `_org_fetch`/`_group_fetch`, anonymous resolution `_anon_org_fetch`) compose their
# own CascadeProbe on top — do not plug FETCH_PROBE as-is into a
# new resolution path of a multi-account connector (review #399 F3).
FETCH_PROBE = CascadeProbe(
    member=lambda s, o, p: ((lambda k: (k, "") if k
                             and not db.member_instance_suspended(s, o, p) else None)(
                                 db.get_member_api_key(s, o, p))),
    member_cross=lambda s, o, p: db.get_member_api_key(s, o, p),
    legacy_user=lambda s, p: credentials_store.get_credential(
        credentials_store.USER, s, p),
    group=lambda g, p: group_store.get_group_secret(g, p),
    org=lambda o, p: org_store.get_org_secret(o, p),
    tenant=lambda t, p: tenant_vault.get_tenant_secret(t, p),
    platform=lambda s, p, o: platform_grant._resolve_platform_grant(s, p, o),
)


def group_secret_map(groups: Optional[list] = None) -> dict:
    """`{group_id: {connectors for which the team holds a secret}}` — ONE read per
    team, never one per (team × connector).

    Two callers ask THIS question on the `/api/me` path: the `group` rung
    of the preloaded probe, and the `team_key_group` hint of `status_for`. The second
    queried the DB per connector (`has_group_secret`), and since it only
    fires on `forbidden` connectors — the majority of a real account — it
    cost 67 round-trips on its own where the inventory was ALREADY loaded next
    to it. Hence the extraction into a named function: both build it each,
    through the same read.

    Each its own, and NOT a map passed from one to the other: `preloaded_presence_probe`
    is a test seam (stubbed by lambda in three files), and adding a
    parameter to it breaks those stubs to save one read PER TEAM — one to three,
    against the sixty-seven this batch removes. Sharing would cost more than it returns.

    ⚠️ Same definition of "holds" as the preloaded probe, on purpose: the presence
    of a row in `list_credentials` — that of `has_credential` too since the
    DB forbids a row without ciphertext (`secret_enc NOT NULL`, #521). On this
    point the map invents nothing — it ALIGNS the hint with the verdict the cascade
    already returns, instead of letting it answer through a path that could diverge.
    """
    from .. import credentials_store as cs

    par_groupe: dict = {}
    for g in (groups or []):
        gid = int(g["group_id"] if isinstance(g, dict) else g)
        par_groupe[gid] = ({r["connector"]
                            for r in cs.list_credentials("group", str(gid))}
                           | {i["connector"] for i in group_store.lent_instances(gid)})
    return par_groupe


def preloaded_presence_probe(sub: str, *, org: Optional[int],
                             groups: Optional[list] = None) -> CascadeProbe:
    """`PRESENCE_PROBE`, but preloaded: the same answers, in a few reads.

    **This is a THIRD PROBE, not a second path.** The walker is not touched
    by a single line: the whole cascade — byo_user gates, cross-org personal instance,
    ORG_SHAREABLE, platform eligibility — stays where it is. Only the
    way the five questions find their answer changes: in memory, from an
    inventory read once, instead of one round-trip per connector.

    ⚠️ **The price of a preloaded probe is staying EQUIVALENT.** It cannot
    drift silently: `tests/test_presence_batch.py` compares it with
    `PRESENCE_PROBE` over all the registry's connectors, same context, and
    demands the SAME verdict. A rung added to the cascade tomorrow breaks this differential
    instead of producing two truths.

    Measured (33 installed connectors, real account): the five probes cost 425 ms
    walking once per connector. What it does NOT cover, and on purpose:
    - `personal_instance_org` is called by the WALKER, not by the probe (one call,
      12 ms) — preloading it would mean touching the walker, which we refuse.

    ⚠️ **09/17: the platform rung is no longer in this list** — the 08/21 note
    above measured ONE call, not the COUNT on a real account: measured
    since (oto cd, prod), `list_platform_instances` was read 31 TIMES per
    `status_for` (once per connector reaching this rung, doubled on the grants
    chain — `grants_chain.platform_rung` re-reads the same table, 0053-L5). The
    computation does not move (chain and legacy remain two separate verdicts); only the
    READ is shared, once for the whole `status_for` via
    `list_all_platform_instances()`."""
    from .. import credentials_store as cs

    membre: set = set()
    suspendues: set = set()
    if org is not None:
        for r in cs.list_credentials(cs.MEMBER, cs.member_id(org, sub)):
            membre.add(r["connector"])
            if (r.get("meta") or {}).get("suspended") in (True, "true"):
                # Suspension only applies to the MONO account (account '') — that is
                # what the original probe queries (`account=""`).
                if not r.get("account"):
                    suspendues.add(r["connector"])

    par_groupe = group_secret_map(groups)

    org_secrets: set = set()
    if org is not None:
        org_secrets = {r["connector"] for r in cs.list_credentials("org", str(org))}

    # TENANT rung (L-keys PR 1): one read, only for a sub of a third-party
    # tenant — `rung_tenant` returns None for a bare sub, and the inventory is not read.
    tenant_secrets: set = set()
    slug = tenant_vault.rung_tenant(sub)
    if slug is not None:
        tenant_secrets = {r["connector"] for r in cs.list_credentials(cs.TENANT, slug)}

    # PLATFORM rung (09/17, see note above): one read for all
    # providers, the `_platform_grant_meta` computation does not move.
    instances_par_provider = cs.list_all_platform_instances()

    return CascadeProbe(
        # The inventory only covers the CONTEXT org: another org (the key of a
        # shared-project beneficiary, set on their side — #480) re-reads at the source.
        member=lambda s, o, p: (PRESENCE_PROBE.member(s, o, p) if o != org
                                else (True, "") if p in membre and p not in suspendues
                                else None),
        # Cross-org: the inventory covers ONLY the active org, so we fall back to the
        # original read. It is one call, not thirty-three: the walker only gets there
        # for single-account `personal_cross_org` connectors.
        member_cross=PRESENCE_PROBE.member_cross,
        legacy_user=PRESENCE_PROBE.legacy_user,  # (#876) same exception as member_cross
        group=lambda g, p: (True if p in par_groupe.get(int(g), ()) else None),
        org=lambda o, p: (True if p in org_secrets else None),
        tenant=lambda t, p: (True if p in tenant_secrets else None),
        platform=lambda s, p, o: platform_grant._platform_grant_meta(
            s, p, o, instances=instances_par_provider.get(p, [])),
    )


def walk_cascade(sub: Optional[str], provider: str, *, org: Optional[int],
                 group: "Optional[int] | Callable[[], Optional[int]]",
                 probe: CascadeProbe, want: str = "auto"):
    """Generator of the winning rungs, IN the cascade's ORDER. Consuming the
    first = resolution (`cascade_winner`); consuming all = status (the levels
    configured beyond the winner remain displayable). Each gate (byo_user,
    ORG_SHAREABLE, personal_cross_org, platform eligibility, `want='byo'`)
    lives HERE — never again in a call-site.

    **Credential delegation** (`Connector.credential_of`): `provider` is first
    normalized to the connector that CARRIES the key (the six unipile channels → `unipile`).
    Doing it HERE and nowhere else is this walker's reason for being: the
    resolution (`_resolve_credential_impl`), the mode mirror (`credential_mode_for`)
    and the status (`status_for`) all three traverse it — so they cannot
    answer three different things to "which key?". Normalizing in each of them
    would reopen exactly the 2026-07-07 divergence (green "org key" card next to
    a red "Blocked"). What the walker does NOT decide, however, is the RIGHT
    to call: activation, ACL and selection stay gated on the BARE name, in the
    caller."""
    provider = providers.credential_provider(provider)
    # SHARED project (#480): the verdict set by `_project=` when the caller does not reach
    # all of the owner's keys on their own. None outside this case — and then
    # nothing below changes. Read from a contextvar: no query here.
    cles = heritage.du_contexte(sub, org)
    # The org whose SHARED rights the caller consumes (org key, org platform
    # access): the context one, except for a beneficiary outside the org to whom
    # nothing was lent.
    org_cles = heritage.org_partagee(org, cles)
    if sub is not None and org is not None and providers.is_byo_user(provider):
        hit = probe.member(sub, org, provider)
        if hit is not None:
            payload, account = hit
            yield CascadeRung("user", credentials_store.MEMBER,
                              credentials_store.member_id(org, sub), payload, account)
    # Cross-org personal instance (#172, amends ADR 0033): per-person connector
    # (unipile) → my key set in ANOTHER org follows me (same sub, zero impersonation).
    # Single-account only. Takes precedence over the shared tiers, like the local key.
    # Also open to the BENEFICIARY of a project of an org they are not a member of
    # (#480): they work there with THEIR keys, and this is where those follow them — including
    # multi-account, read by the MEMBER probe (account selection, suspension)
    # on the org where they set them.
    beneficiaire = heritage.hors_org(cles) and providers.is_byo_user(provider)
    if sub is not None and (beneficiaire or (providers.is_personal_cross_org(provider)
                                             and not _is_multi_account(provider, org))):
        pio = personal_instance_org(sub, provider, exclude_org=org)
        if pio is not None:
            if beneficiaire:
                payload, account = probe.member(sub, pio, provider) or (None, "")
            else:
                payload, account = probe.member_cross(sub, pio, provider), ""
            if payload is not None:
                yield CascadeRung("user", credentials_store.MEMBER,
                                  credentials_store.member_id(pio, sub), payload,
                                  account, via="cross_org")
    # LEGACY scope (#876): ONLY the REQUESTING sub's row, closed list.
    if sub is not None and provider in LEGACY_USER_SCOPE_PROVIDERS:
        payload = probe.legacy_user(sub, provider)
        if payload is not None:
            yield CascadeRung("user", credentials_store.USER, sub, payload)
    if provider in ORG_SHAREABLE_PROVIDERS:
        # `group` accepts a zero-arg callable (LAZY resolution of the active
        # team): the generator stops at the first winning rung, so a found
        # member key never costs the DB lookup of `current_group`
        # (iso-behaviour with the old path; seen test_call_axes_account).
        g = group() if callable(group) else group
        if g is not None:
            hit = probe.group(g, provider)
            if hit is not None:
                # Multi-account fetch probe: (payload, account) — the presence
                # probe answers a boolean, the rung stays mono ('').
                payload, account = hit if isinstance(hit, tuple) else (hit, "")
                yield CascadeRung("group", "group", str(g), payload, account)
        # The OWNING team of a shared project, lent through inheritance (#480).
        herite = cles.groupe_herite if cles is not None else None
        if herite is not None and herite != g:
            hit = probe.group(herite, provider)
            if hit is not None:
                payload, account = hit if isinstance(hit, tuple) else (hit, "")
                yield CascadeRung("group", "group", str(herite), payload, account,
                                  via="heritage")
        # The ORG rung requires membership, or inheritance (#480): until then it
        # was guarded by nothing, and `_project=` let a non-member in.
        if org_cles is not None:
            hit = probe.org(org_cles, provider)
            if hit is not None:
                payload, account = hit if isinstance(hit, tuple) else (hit, "")
                yield CascadeRung("org", "org", str(org_cles), payload, account,
                                  via="heritage" if heritage.hors_org(cles) else "local")
        # TENANT tier (L-keys PR 1, ADR 0052): the shared key of the
        # CALLER's tenant — read from their qualified sub, never from the org's
        # attachment (batch L1). `rung_tenant` returns None for a bare sub (primary tenant: its shared
        # keys are the platform instances) and for the anonymous — the rung
        # is then not probed at all, so it costs nothing where it can find
        # nothing. Under the ORG_SHAREABLE gate like the team and the org: it is a shared
        # key. Served BEFORE the platform: closer to the caller.
        slug = tenant_vault.rung_tenant(sub)
        if slug is None and sub is None and org is not None:
            # ANONYMOUS (ADR 0032, L-keys PR 2): no identity ⟹ the tenant is only read
            # through a LIVE tenant→org edge — never from the org's
            # attachment (batch L1). Without an edge, the anonymous keeps its `org > platform` cascade.
            slug = grants_chain.tenant_for_org(org, provider)
        if slug is not None:
            hit = probe.tenant(slug, provider)
            if hit is not None:
                # The tenant→org edge (0053, PR 2) — read AFTER the probe, so never
                # without a key: SILENT ⟹ the key serves (PR 1); GRANTS ⟹ it serves, the
                # budget is settled at resolution (`tenant_budget`); REFUSES ⟹ the
                # rung is SKIPPED and the org falls back to the platform.
                # On the KEYS org (#480): the tenant→org edge is a right of the
                # org — neither its budget nor its revocation reach a beneficiary
                # outside it to whom nothing was lent.
                verdict = grants_chain.tenant_rung(slug, provider, org_cles)
                if verdict is None or verdict.granted:
                    payload, account = hit if isinstance(hit, tuple) else (hit, "")
                    yield CascadeRung("tenant", credentials_store.TENANT, slug, payload,
                                      account, via="grant" if verdict else "local")
    if want != "byo":
        con = providers.connector_for_provider(provider)
        if con is not None and "platform" in con.auth_modes:
            grant = probe.platform(sub, provider, org_cles)
            if grant:
                yield CascadeRung("platform", credentials_store.PLATFORM,
                                  grant.get("label"), grant)


def cascade_winner(sub: Optional[str], provider: str, *, org: Optional[int],
                   group: "Optional[int] | Callable[[], Optional[int]]",
                   probe: CascadeProbe,
                   want: str = "auto") -> Optional[CascadeRung]:
    """First winning rung, or None if nothing resolves."""
    return next(walk_cascade(sub, provider, org=org, group=group,
                             probe=probe, want=want), None)
