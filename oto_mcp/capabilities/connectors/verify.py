"""Capability "test a connector's connection" (probe framework, ADR 0009).

Resolves the credential (effective cascade, or an explicit org key) and runs the
connector's registered probe (`connector_verify`). An authentication failure IS the
result (`{ok:false, error}`), never a 500 — same spirit as the tool test bench
(`my_tool_call`). The provider message is already cleaned by the probe; here we only
extract the one from a `McpError` (e.g. missing Zoho data center).
"""
from __future__ import annotations

import time
from typing import Literal, Optional

from ...mcp_errors import McpError
from pydantic import BaseModel, ConfigDict, Field

from ... import access, credentials_store, providers, status_hints
from ...connectors import health as connector_health
from ...connectors import verify as connector_verify
from .._authz import ORG_ADMIN, ORG_MEMBER
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding)
from ._level_doc import DOC_LEVEL as _DOC_LEVEL


class VerifyInput(BaseModel):
    provider: str                              # path {provider}
    level: Literal["auto", "org"] = "auto"     # auto = effective credential; org = the org's key
    # Tells apart several instances of the same connector at the same level (multi-account,
    # same vocabulary as `ConnectorInstance.account` — `instances.py`): `""` = the
    # default key. Under `level="org"`: the account of the org key; under `auto`: the
    # account to pick when the winning tier of the cascade holds several (without
    # it, a probe right after adding a 2nd named account has no way to tell
    # which one to test — refused with `account_required`).
    account: str = ""


class VerifyResult(BaseModel):
    """Verdict of a connection probe. An authentication failure IS the
    result (200 + `ok:false`), never a 500 — a client that only looks at the
    HTTP code would always conclude success."""
    ok: bool
    provider: str
    # Probe duration. Is **0** without having probed anything when `pending` is true.
    elapsed_ms: int
    # WHICH instance answered, DERIVED from the same entity as `ref` so that they
    # cannot contradict each other. Under `level="auto"` the cascade may have fallen
    # one tier: `ok:true` alone does not distinguish "my personal key works" from "my
    # personal key failed, the org's key is the one answering". `platform` = platform
    # grant, which has no vault row.
    level: Literal["member", "group", "org", "tenant", "platform"] = Field(
        description=_DOC_LEVEL)
    # `<level>:<entity_id>:<provider>` — e.g. `org:2:salesforce`. At the platform
    # tier the `entity_id` is the key's LABEL (ADR 0044 §F: no more
    # surrogate id), not an integer: `platform:serper-shared:serper`.
    ref: str
    # ⚠️ Carries TWO meanings depending on `pending`. Under `pending:true` it is NOT an
    # error but the NEXT STEP to take (`status_hints.next_action`); otherwise
    # it is the probe's failure message. ABSENT when `ok:true`.
    error: Optional[str] = None
    # Present (and true) ONLY on a deliberately incomplete TWO-step connection
    # (app set up, consent to come): nothing was probed.
    # `ok:false` + `pending:true` = "saved, the rest happens elsewhere", not an
    # invalid credential — confusing the two reopens the form on a correction
    # that cannot be made.
    pending: Optional[bool] = None

    # WHAT THE PROBE MEASURED (oto#57) — `auth`: the key authenticates, and nothing
    # more; `auth+quota`: it authenticates AND there is enough left to work with.
    #
    # ⚠️ An `ok:true` under `coverage:"auth"` says NOTHING about the balance. That is what
    # 04/09/2026 cost: an all-green preflight, then a 402 after four spaces,
    # four tables and 28 rows were created. **The probe had not lied** — it had
    # reported a green that did not mean what people thought. The field exists so
    # that a caller knows what it does not know.
    #
    # ⚠️ `null` = no probe is declared for this connector. That is not
    # "covers nothing": in one case we could not measure, in the other we
    # measured authentication. The two call for different conduct.
    coverage: Optional[str] = None

    # WHY it does not work — `ok` | `unauthorized` | `no_quota` | `unknown`
    # (oto#57). Three causes that call for OPPOSITE conduct: replace the key,
    # top up the account, or above all do nothing. The boolean `ok` did not
    # tell them apart, and it is still served as is — a served contract is not hardened
    # in place, it is doubled.
    #
    # ⚠️ `unknown` reads "I do not know", NEVER "nothing serious". It is the
    # most frequent verdict as long as probes do not raise typed errors, and
    # mistaking it for a diagnosis would do the harm this field fixes.
    verdict: Optional[str] = None
    # The conduct to follow, in plain words. A diagnosis that does not say what to do sends
    # people searching — that is how someone retried a valid connection six times.
    # Absent when all is well.
    next_step: Optional[str] = None

    # WHO the key authenticates as at the provider — the only surface that says it.
    # Today Slack: `{"bot": {app_id, bot_id, team, team_id, url, user,
    # user_id}, "user": {...}}`, one block per token set, reduced to what the
    # provider actually returned.
    #
    # ⚠️ This field answers "is it still the same application as yesterday?", and
    # nothing else. The platform compares NOTHING: it never kept yesterday's value.
    # A key replaced by that of another application authenticates perfectly yet
    # starts without any of the previous one's channel memberships — the
    # vault only sees a healthy key. It is up to the caller to keep this value and
    # compare it; if it does not, nobody will do it for it.
    #
    # ⚠️ Absent = this connector does not expose it, NEVER "the identity has not changed".
    identity: Optional[dict] = None
    # Per API family (HubSpot today, `coverage:"auth+scopes"`): `granted` | `missing` |
    # `unknown`, plus the scope names to add. A missing family does NOT turn `ok`
    # false — the key works for the rest (oto#69: a partial scope is not the
    # connection's verdict); it is what an agent reads before a call that would 403.
    scopes: Optional[dict] = None


class MemberProviderStatus(BaseModel):
    """A member's `ProviderStatus` entry for a connector — the SAME shape that
    their own card reads, replayed by an org_admin.

    The set of keys depends on the connector FAMILY (quota-based keyed, BYO with
    declared fields, browser session, OAuth): `quota_*` only exists with a
    platform tier, `session_set_at`/`identity_*` only for a browser
    session, etc. Hence the openness to additional fields — only `mode` and
    the four presence booleans are served by all families."""
    model_config = ConfigDict(extra="allow")

    # `forbidden` = nothing resolves FOR THIS MEMBER; it does not say why (missing
    # option, activation cut, RBAC) — `connectors.me` is what disambiguates.
    mode: str
    user_key_configured: bool
    group_secret_configured: bool
    org_secret_configured: bool
    platform_key_label: Optional[str] = None
    quota_used_today: Optional[int] = None
    # `null` = no platform tier OR unlimited quota (convention: 0 unlimited
    # is translated to `null` so the UI shows "∞", not "/0").
    quota_daily: Optional[int] = None
    # Team key "in reach" without being active — filled ONLY when
    # `mode == "forbidden"`: its presence says "a key exists, it must be pinned".
    team_key_group: Optional[dict] = None


class ConnectorEffectForMember(BaseModel):
    """Verdict of a connector REPLAYED for a named member (M4): what THEY see,
    computed against THEIR org (never the requester's context, ADR 0023)."""
    provider: str
    member: str                               # sub of the targeted member
    # ⚠️ `null` = this connector has NO status entry (name outside the catalog, or
    # a family with no credential) — not "access denied". A front that renders null
    # as a block invents a verdict.
    status: Optional[MemberProviderStatus] = None


def _ref(entity_type: "str | None", entity_id: "str | None", provider: str) -> str:
    """Readable identifier of the probed instance. `None` = platform grant: it has no
    vault row, but we still need to be able to NAME it in the result."""
    if entity_type is None:
        return f"platform:{provider}"
    return f"{entity_type}:{entity_id}:{provider}"


def _fields_config_scope(ctx: ResolvedCtx, inp: VerifyInput) -> tuple[dict, dict, "tuple | None", dict, "tuple | None"]:
    """(fields, config, health SCOPE, probed INSTANCE, write TARGET) depending on the level.

    `instance` = which key was ACTUALLY tested (`level` + `ref`). Without it, an
    `ok:true` at level `auto` is ambiguous: the cascade may have fallen one tier, and we
    cannot tell "my personal key works" from "my personal key failed, the org's key
    is the one answering". That is precisely the case where confirmation matters.

    `config` = NON-secret satellites paired with the key (public meta: unipile
    dsn…) — a probe to an endpoint whose host depends on the key MUST take it into account.
    `health scope` = where to persist the result (`meta.health_ko` + `meta.health_reason`):
    `(entity_type, entity_id, account)` of the row ACTUALLY tested, as long as it
    is not shared beyond the caller's org — otherwise None.
    ⚠️ It only applied to the MEMBER tier until 2026-09-03: a `level="auto"`
    that resolved an ORG key — the only possible tier for a `byo_org`-only connector
    like `linear` — wrote NOTHING. The user read `ok:false` on the probe and
    found their card green behind them (#541). The TENANT tier and the PLATFORM
    key remain excluded, shared by entire orgs: one member's network
    hiccup has no business painting them red for everyone.

    - `auto` (user card): the EFFECTIVE credential (cascade user > team > org >
      platform). `emit_on_failure=False`: a probe must not pollute monitoring.
    - `org` (org card): the org's key specifically activated/consulted (a personal key
      would mask it in the cascade). `ctx.org_id` is injected by authz (IDOR-safe)."""
    if inp.level == "org":
        account = inp.account or ""
        row = credentials_store.get_credential_with_meta(
            "org", str(ctx.org_id), inp.provider, account)
        if not row:
            if account:
                raise AuthzDenied(400, "no_org_credential",
                                  f"no org key set for this connector under "
                                  f"account “{account}”.")
            raise AuthzDenied(400, "no_org_credential",
                              "no org key set for this connector.")
        return (credentials_store.unpack_secret(inp.provider, row["secret"]),
                credentials_store.public_meta(row.get("meta")),
                ("org", str(ctx.org_id), account),
                {"level": "org", "ref": _ref("org", str(ctx.org_id), inp.provider)},
                ("org", str(ctx.org_id), account))
    try:
        rc = access.resolve_credential(
            inp.provider, want="auto", sub=ctx.sub, account=inp.account or None,
            emit_on_failure=False,
        )
    except access.CompteAmbigu as e:
        # Several accounts at the winning tier, none named or default: a NAMED
        # refusal, not an exception that would come out as a bare 500 (without CORS — the browser
        # only read "Failed to fetch" there, even though the key was properly set).
        raise AuthzDenied(400, "account_required", e.error.message) from e
    etype, eid = getattr(rc, "entity_type", None), getattr(rc, "entity_id", None)
    scope = ((etype, eid, getattr(rc, "account", "") or "")
             if etype in connector_health.FLAGGABLE_SCOPES and eid else None)
    # `level` and `ref` are DERIVED from the same source — the entity — so that they
    # cannot contradict each other. The previous version exposed `rc.mode`, whose
    # vocabulary differs (`user` where the entity, the `ref` and `oto_instance op=list`
    # say `member`): two words for the same object in the same response, and any
    # code comparing this `level` to the list's broke. Reported on 03/08.
    instance = {"level": etype or "platform", "ref": _ref(etype, eid, inp.provider)}
    # WRITE target for a probe with side effects (rotation) — None for a platform
    # grant, which has no vault row to rewrite.
    cible = (etype, eid, getattr(rc, "account", "") or "") if etype else None
    return rc.fields, rc.config, scope, instance, cible


async def _verify(ctx: ResolvedCtx, inp: VerifyInput) -> dict:
    probe = connector_verify.probe_for(inp.provider)
    if probe is None:
        raise AuthzDenied(400, "verify_unavailable",
                          f"no connection test for “{inp.provider}”.")
    fields, config, scope, instance, cible = _fields_config_scope(ctx, inp)
    # TWO-step connection: a DELIBERATELY incomplete credential (app set up,
    # consent to come) is not an input error — probing it would return a
    # failure, and the dashboard form would stay open on a correction that
    # cannot be made ("connect does nothing", experienced 28/07). We return the STATE, not
    # a verdict: `pending=True` tells the front "it is saved, the next step is
    # elsewhere". Same source as the card's verdict (`status_hints`).
    st = status_hints.credential_state(inp.provider, fields)
    if st is not None and not st.complete:
        # ⚠️ No `verdict` here, and that is deliberate: NOTHING was probed. Setting
        # `unknown` would suggest a measurement that failed, when the credential
        # is simply incomplete — `pending` already says so, and better.
        return {"ok": False, "pending": True, "provider": inp.provider,
                "error": st.next_action, "elapsed_ms": 0,
                "coverage": connector_verify.couverture(inp.provider), **instance}
    started = time.monotonic()
    ok, error, verdict, mesures = True, None, connector_verify.OK, {}
    try:
        # A single place runs the probes (`connectors.verify.executer`): off
        # the event loop, under a time bound, and it carries the resolution of
        # `instance` — under rotation, probing CONSUMES the token, and the
        # replacement must be rewritten on the tested row, not on the one the
        # cascade would have chosen. This block duplicated that gesture: fixing one
        # left the other (oto-backend#867, batch 2).
        mesures = await connector_verify.executer(probe, fields, config, cible)
    # noqa: SILENT — the auth error IS the probe's result, returned to the caller
    except Exception as e:  # noqa: BLE001 — the auth error IS the result
        ok = False
        error = e.error.message if isinstance(e, McpError) else str(e)
        # Classified on what the exception CARRIES (raised type, then `status_code`), never
        # on the words of its message: a classification built on text changes meaning
        # at the first upstream reformatting, without anyone noticing.
        verdict = connector_verify.classer(e)
    # The probe IS the "health check" (easy read) → its verdict feeds the health flag.
    # `record_health` = shared helper (`connectors/health.py`, oto#25 batch b2): same
    # two lines as before under `_record_health`, extracted so that other modules
    # (salesforce, zoho) reuse the SAME scope guard without
    # redefining it each on their own side.
    # ⚠️ Normalized carrier (delegation, six Unipile channels): the vault row is
    # filed under `unipile`, never under `linkedin_unipile` — writing under the BARE name
    # would target a row that does not exist and `update_meta` would fail silently
    # (0 rows touched, no exception). Same normalization as `probe_for`.
    # The VERDICT is persisted too (`no_quota`/`unauthorized`/`unknown`): it is what
    # makes the card say "top up" rather than "set the key again" (`readiness`).
    connector_health.record_health(providers.credential_provider(inp.provider),
                                   scope, ok, error, None if ok else verdict)
    out = {"ok": ok, "provider": inp.provider,
           "elapsed_ms": int((time.monotonic() - started) * 1000),
           # What this verdict is WORTH: served with it, never beside it. A client that
           # reads `ok` without reading this believes it knows more than it does.
           "coverage": connector_verify.couverture(inp.provider),
           "verdict": verdict,
           # WHICH instance answered — see `_fields_config_scope`.
           **instance}
    # What the probe MEASURED, when it measures something (`auth+quota`).
    # The balance lives HERE and nowhere else: the connector's card only carries
    # the verdict and its date. A displayed figure would promise a freshness that the
    # platform only holds by querying, and querying costs — a dated verdict
    # says exactly what we know, no more, no less.
    if mesures:
        out.update(mesures)
    if not ok:
        out["error"] = error
        # The conduct, served WITH the diagnosis: the three causes call for opposite
        # moves, and "do not retry" is one of them.
        conduite = connector_verify.CONDUITE.get(verdict)
        if conduite:
            out["next_step"] = conduite
    return out


CAP_DOC = (
    "Test whether a connector's configured credential actually authenticates "
    "(side-effect-free probe), returning {ok, error}. Use it to diagnose a connector "
    "that is set but not working (wrong region, expired token…) before reporting a gap. "
    "'auto' tests the credential that resolves for you; 'org' tests the org shared key "
    "— pass `account` to pick one of several instances of the same connector (e.g. "
    "several companies each with their own key), under 'org' or under 'auto' when the "
    "resolved level holds several named accounts with no default (otherwise refused "
    "`account_required`); default `\"\"` for the default one. "
    "The reply names the instance actually probed (`level` + `ref`) — under 'auto' the "
    "cascade may have fallen through to a shared key, and `ok` alone would not say so. "
    "⚠️ READ `coverage` WITH `ok`: it says what the probe actually measured. "
    "`auth` = the key authenticates, and NOTHING about credit or quota — an `ok:true` "
    "there does not mean the account can still work. `auth+quota` = it also checked "
    "there is something left to spend. `null` = this connector declares no probe at "
    "all, which is not the same as 'nothing to check'. `auth+scopes` = it also read, per API family, which scopes the token holds (`scopes`; a missing family keeps `ok:true`). A preflight built on `ok` alone "
    "reports green on an exhausted account and the work fails mid-flight, after side "
    "effects."    "⚠️ `verdict` says WHY when it fails — `unauthorized` (replace the key or widen "
    "its scope; adding another one changes nothing), `no_quota` (the key is fine, the "
    "balance is empty: top up, do NOT reconnect), or `unknown` (the probe failed "
    "without saying why — read `error` as it stands). `next_step` spells out the move. "
    "⚠️ `unknown` means 'I do not know', never 'nothing serious'. "
    "⚠️ `identity` (Slack today) names WHO the key authenticates as at the provider — "
    "app_id / bot_id / team / user, one block per token posed. It exists to answer "
    "'is this still the same app as yesterday?', because a key swapped for ANOTHER "
    "app's tokens authenticates fine and starts with none of the channel memberships "
    "the previous one had — the vault only ever sees a healthy key. **oto compares "
    "nothing and keeps no history**: record this value yourself if you want to notice "
    "the change. Its absence means this connector does not expose it, never 'unchanged'."
)

class EffectForMemberInput(BaseModel):
    provider: str                              # path {provider}
    member: str                                # query ?member=<sub>: the target member


def _effect_for_member(ctx: ResolvedCtx, inp: EffectForMemberInput) -> dict:
    """M4 (connector spec): replays a connector's verdict FOR an org member
    (org admin) → "Effect for: [member]". The org is passed EXPLICITLY to `status_for`
    (ADR 0023: never a third party's `current_org`). Anti-IDOR: the target must belong to
    the active org. Returns the member's `ProviderStatus` entry for this connector (the front
    passes it to `connectorVerdict` to display the same sentence the member would see)."""
    org = ctx.org_id
    if org is None:
        raise AuthzDenied(400, "no_active_org", "No active org.")
    from ... import roles
    if not roles.is_org_member(inp.member, org):
        raise AuthzDenied(404, "not_a_member", "This member does not belong to this org.")
    st = access.status_for(inp.member, org=org)
    return {"provider": inp.provider, "member": inp.member,
            "status": (st.get("providers") or {}).get(inp.provider)}


from ..registry import CAPABILITIES  # noqa: E402

CAPABILITIES += [
    Capability(
        key="connectors.verify", handler=_verify, Input=VerifyInput, authz=ORG_MEMBER,
        Output=VerifyResult,
        description=CAP_DOC,
        errors=(DeclaredError(400, "no_org_credential",
                              "no org key set for this connector: there is "
                              "nothing to verify"),
                DeclaredError(400, "verify_unavailable",
                              "this connector declares no verification "
                              "probe"),
                DeclaredError(400, "account_required",
                              "several accounts of this connector at the "
                              "resolved tier, with no single default: pass `account` "
                              "(the name of the account to test)"),),
        rest=RestBinding("POST", "/api/me/connectors/{provider}/verify"),
    ),
    Capability(
        key="connectors.effect_for_member", handler=_effect_for_member,
        Input=EffectForMemberInput, authz=ORG_ADMIN, Output=ConnectorEffectForMember,
        description="Org admin: replay a connector's verdict AS a given org member (M4). "
                    "Returns that member's ProviderStatus entry for {provider}, org scoped.",
        errors=(DeclaredError(400, "no_active_org",
                              "no context org to judge the effect"),
                DeclaredError(404, "not_a_member",
                              "the targeted person is not a member of this org"),),
        rest=RestBinding("GET", "/api/me/connectors/{provider}/effect"),
    ),
]
