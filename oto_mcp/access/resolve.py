"""The REAL resolution of a credential — the hot path (ADR 0024/0038).

`resolve_credential` is the public view; `_resolve_credential_impl` walks the
cascade ONCE with the fetch probe (only the winner is decrypted) and returns
a `ResolvedCredential` (key + origin + non-secret config). Three paths
short-circuit the walk, in this order of specificity: the instance pinned
by the call (`_instance=`), the one bound by the project, then the cascade.
`resolve_anon._resolve_credential_anon` is its org-only mirror, for the published
MCP endpoint (ADR 0032) where there is nobody whose default account could be
taken; the returned type lives in `resolved_credential` (both extracted from here on
2026-08-29, 500-line ratchet, #584).

Depends on everything below it: `scope` (context, project pins),
`rbac` (instance guard, error hint), `cascade` (the walker),
`quotas` (the platform tier's ceiling). The thin views built on top of it
(`resolve_api_key`, `resolve_credential_fields`, `platform_quota_hint`…) live in
`views`.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

# `links`: where to set a key ACCORDING TO THE ACCOUNT'S PRODUCT — no template, no link
# (oto-backend#935, cf. `links.ou_poser_la_cle`).
from .. import links
from .. import (providers, credentials_store, db, group_store, instance_refs, org_store,
                session_org, tenant_vault)
from ..connectors import cle_perso
from . import (cascade, chain_shadow, credits, heritage, indices, limite_du_jour,
               platform_rate, prets, quotas, rbac, resolve_anon, scope, tenant_budget)
from .resolved_credential import ResolvedCredential

logger = logging.getLogger(__name__)


class CredentialUnavailable(McpError):
    """No reachable key; distinct from an ambiguous account or an access refusal."""


def resolve_credential(provider: str, want: str = "auto",
                       sub: Optional[str] = None, *,
                       account: Optional[str] = None,
                       emit_on_failure: bool = True,
                       check_usage: bool = True,
                       units: int = 1) -> ResolvedCredential:
    """Public view of the resolution. On **failure** (actionable McpError — credential
    missing / quota exceeded / RBAC access refused), emits a monitoring event
    `kind='connector'` into the unified stream (ADR 0017) BEFORE re-raising: it is THE
    signal of a connector that does not resolve for a user/org, invisible until now
    (an active account without a valid key showed up nowhere). `emit_on_failure=False`
    for **probes** that swallow the McpError, so as not to skew the signal.
    `check_usage=False` to CONFIGURE a connection: same access guards and
    account choice, but no usage quota or tenant debit. A tool execution
    always keeps the default `True`. `units` = consumption the call is about to debit
    (batch size): the shared key's quota is checked for THIS amount, not
    for 1 (`used + units > limit` refuses). Cascade: see `_resolve_credential_impl`."""
    if sub is None:
        # ANONYMOUS MCP endpoint (ADR 0032): no sub → resolution against the project's
        # owning org (org secret > org grant > open platform key),
        # without per-sub quota (the subdomain's rate limit bounds abuse).
        from .. import subdomain_project
        anon = subdomain_project.current_anon_context()
        if anon is not None:
            return _note_resolved_instance(
                resolve_anon._resolve_credential_anon(provider, want, anon.org_id, check_usage))
    sub = sub or scope.current_user_sub_or_raise()
    try:
        resolved = _resolve_credential_impl(provider, want, sub, account=account,
                                            check_usage=check_usage, units=units)
        # A lent Google/Microsoft account serves only the services its loan lends —
        # checked HERE, at the single resolver, whatever path served the loan (`_account=`
        # at a team or org rung, `_instance=`, the Microsoft loan resolver).
        prets.exiger_service_prete(sub, provider, resolved)
    except McpError:
        if emit_on_failure:
            _emit_connector_failure(provider, sub)
        raise
    if check_usage and resolved.is_platform:
        # The call goes through a PLATFORM key it is about to consume: the edge of the
        # call settles it (`reglement.regler_appel`) with the `units` the tool notes.
        # Not on a connection (`check_usage=False`), which consumes nothing — nor on
        # the anonymous path above, which has no sub to meter.
        session_org.note_call_trace(platform_carrier=providers.credential_provider(provider))
    return _note_resolved_instance(resolved)


def _note_resolved_instance(rc: ResolvedCredential) -> ResolvedCredential:
    """Adds to the call's trace the ref of the vault row that ACTUALLY served
    (allowlist `server._TRACED_ARGS`) — the fingerprint the journal did not carry.

    The journal said which tool and which org; never UNDER WHICH KEY. Yet that is
    precisely the question of an access switchover ("did the call go through the
    edge or the old path?") and of a credential incident
    ("which instance was called?"). No-op outside an MCP call (`note_call_trace`).

    ⚠️ NOT best-effort: what it notes DECIDES (`key_mode` bills, `credential_row` names
    the payer of an Apify run, `tools/apify._org_qui_prete`). The body is pure (a ref
    projected from the row, a write into the call's trace); a failure here is a bug,
    and it RAISES — swallowed, it turned the readers' verdict into a lottery."""
    ref = instance_refs.ref_for_credential(   # ONE ref per row: its CARRIER's
        rc.entity_type or "", rc.entity_id or "",
        providers.credential_provider(rc.provider), rc.account)
    # `instance` = the fingerprint for the JOURNAL. `resolved_*` = what lets us tell
    # the AGENT on return from the call (`_account` echo, `CallContextMiddleware`):
    # without it, it posts to one of its two workspaces without ever knowing which.
    # The connector is noted WITH the account: a composite tool may resolve an
    # auxiliary credential, and the echo must only announce the connector called.
    # `key_mode` = UNDER WHICH KEY the call goes through (`user|group|org|tenant|
    # platform`). Set here, at the SINGLE resolver, so every keyed tool
    # carries it without any tool having to think about it — and a connector added
    # tomorrow will get it for free.
    # What it decides: the partner's billing consumer (external repo)
    # bills ONLY the `platform` mode.
    # A customer on THEIR own key already pays the provider; counting credits
    # on top makes no sense (partner's ruling, 09/09).
    # ⚠️ The `mode` and not `is_platform`: the boolean collapses user/group/org/
    # tenant into a single "no", whereas these are four origins an
    # invoice may need to distinguish.
    # `credential_row` = the vault row ITSELF (not its ref): it is what
    # the health tracking marks when upstream refuses for lack of credits, and
    # clears on first success (`connectors.health.suivre_appel`). Neither journaled
    # (outside `_TRACED_ARGS`) nor billed.
    session_org.note_call_trace(instance=ref, resolved_connector=rc.provider,
                                resolved_account=rc.account,
                                key_mode=rc.mode,
                                credential_row=(rc.entity_type, rc.entity_id,
                                                rc.provider, rc.account or ""))
    return rc


def _emit_connector_failure(provider: str, sub: str) -> None:
    """Best-effort: a `tool_calls(kind='connector', ok=False)` row = "credential
    resolution failed for this provider/sub". Never blocking, never an
    exception that would mask the original McpError (monitoring does not break the service)."""
    try:
        org = scope.current_org(sub)
    # noqa: SILENT — the usage signal never breaks the resolution it observes
    except Exception:
        org = None
    try:
        db.insert_tool_call({
            "kind": "connector", "tool": provider, "sub": sub, "org_id": org,
            "ok": False, "error": "credential_resolution_failed",
        })
    except Exception:  # noqa: BLE001
        logger.debug("connector failure emit failed", exc_info=True)


def _resolve_credential_impl(provider: str, want: str, sub: str,
                             account: Optional[str] = None, *,
                             check_usage: bool = True,
                             units: int = 1) -> ResolvedCredential:
    """Single substrate resolver (ADR 0024): walks the EXACT cascade
    user > active group > active org > tenant [> platform grant] **once** and returns
    the winning credential (key + origin + config). `want="byo"` short-circuits the
    platform tier (byo-only semantics of `resolve_credential_fields`);
    `want="auto"` includes the platform grant + quota (semantics of `resolve_api_key`).
    Explicit `sub` = usable OUTSIDE an MCP context (REST routes); None = current sub.
    `account` selects the account at the MEMBER tier in multi-account ("2 Zoho") —
    None ⇒ project pin, otherwise single auto account, otherwise McpError (see below).
    Raises an actionable McpError if nothing resolves."""
    sub = sub or scope.current_user_sub_or_raise()

    # EXPLICIT instance of the call (`_instance=`, ADR 0038 §C/B6): if the pinned ref
    # targets THIS provider, we resolve EXACTLY that vault row — never a
    # fallback (a requested instance that does not resolve = actionable error, not
    # another identity). A ref for ANOTHER provider is ignored here (it did not target
    # this resolution — e.g. auxiliary resolution of a composite tool).
    # ⚠️ Compared on the CARRIER, both sides (a `…:outlook:…` ref is the `microsoft`
    # row): a ref names a VAULT row, and the six unipile channels have none (their keys
    # live under `unipile`). The bare name would silently ignore the pin (the call would
    # fall back to the cascade, thus potentially onto ANOTHER key than the one requested).
    porteur = providers.credential_provider(provider)
    pinned = session_org.current_call_instance()
    if pinned is not None and providers.credential_provider(pinned.connector or "") == porteur:
        # The vault READ names the carrier, like the comparison above:
        # the pinned row is stored under it, and `require_credential` REFUSES a
        # delegator's name. Passing the bare name here raised a raw `ValueError` on
        # every channel call made under a pin — the pin was recognized then lost.
        return _resolve_pinned_instance(porteur, sub, pinned)

    # PROJECT binding (ADR 0038 B5): the call's project (`_project=`) binds an
    # instance for this provider → HARD resolution, RE-GUARDED for the CALLER (the
    # binding was guarded for whoever set it; the caller of a shared project
    # may be another member). Explicit `_instance=` (above) takes precedence — the
    # most specific token of the call.
    bound = scope.project_pinned_instance(porteur)
    if bound is not None:
        if not heritage.instance_heritee(sub, bound):   # lent by a share (#480)
            rbac.guard_instance_access(sub, bound)
        return _resolve_pinned_instance(porteur, sub, bound)

    # MEMBER scope (ADR 0033): "my key" exists ONLY in the context org —
    # set in org A, it does not resolve from org B. The org is resolved via
    # the `current_org` seam (MCP session ?? consultation ?? home, ADR 0023) BEFORE
    # the first tier: no more org-agnostic per-user credentials.
    active_org = scope.current_org(sub)

    # Account NAMED by the caller — explicit account (param) > call axis
    # `_account=` (#108) > project pin — resolved ONCE, before the walk:
    # it serves every tier (`_pick_account`) AND the post-walk guard (a
    # named account not found anywhere RAISES, never a fallback — review #399 F2).
    # None = nothing named (automatic selection per tier).
    # Pin read on the called connector, then on its CARRIER: a link on the Google
    # ACCOUNT card pins every service that borrows it (#1160).
    named_account = None
    if cascade._is_multi_account(provider, active_org):
        named_account = (account if account is not None
                         else session_org.current_call_account()
                         or scope.project_pinned_identity(provider)
                         or (scope.project_pinned_identity(porteur)
                             if porteur != provider else None))

    def _pick_account(entity_type: str, entity_id: str, mprov: str, where: str,
                      scope: Optional[str] = None) -> tuple:
        """The EFFECTIVE account of a multi-account tier = named account (cf.
        `named_account`) > single auto account > set default (`oto_identity(op='set')`)
        > McpError — never a silent fallback to ANOTHER account (anti-impersonation).
        '' = legacy mono. Returns `(account, explicit)`: an account NAMED by the
        caller may live at a lower tier (Phase 2: the org "had" it, the
        member did not) — the tier that does not have it hands over, and it is the
        POST-WALK guard that raises if it exists nowhere. An automatically chosen
        account is never looked for elsewhere."""
        if named_account is not None:
            return named_account, True
        return cascade._shared_auto_account(entity_type, entity_id, mprov, where, scope), False

    def _not_found(eff: str, mprov: str) -> McpError:
        noun = cascade.account_noun(mprov).capitalize()
        return cascade.CompteIntrouvable(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"{noun} `{eff}` not found for `{mprov}` — check with "
                f"oto_identity(op='list'), or set it"
                f"{links.ou_poser_la_cle(sub, org=active_org)}."
            )), eff)

    def _member_fetch(msub: str, morg: int, mprov: str) -> Optional[tuple]:
        """MEMBER fetch probe: account selection in multi-account ("2 Zoho"),
        cf. `_pick_account`. An explicit/pinned account not found RAISES (we do not
        act under another identity).

        A SUSPENDED instance (batch 2 / ADR 0044 §KeyStack) is treated as
        absent: the member rung passes its turn and the level below
        (group/org/platform) takes over — same verdict as the PRESENCE/FETCH probes,
        otherwise the real resolution contradicts what the KeyStack
        announces (#401). Suspending is a member's act on THEIR key: the handover is
        the contract, not an impersonation. (SUSPENDED NAMED account: the handover never
        goes as far as the platform key — the named account's post-walk guard
        raises, review #399 F2.)"""
        if not cascade._is_multi_account(mprov, morg):
            key = db.get_member_api_key(msub, morg, mprov)
            if key and db.member_instance_suspended(msub, morg, mprov):
                return None
            return (key, "") if key else None
        eff, explicit = _pick_account(credentials_store.MEMBER,
                                      credentials_store.member_id(morg, msub),
                                      mprov, "in this org")
        key = db.get_member_api_key(msub, morg, mprov, eff)
        if eff and not key and not explicit:
            raise _not_found(eff, mprov)
        # Suspension PER account (the `account` of providers.instances.suspend):
        # an existing but set-aside key skips the rung — after the
        # "not found" check, which keeps its semantics (absent ≠ suspended).
        if key and db.member_instance_suspended(msub, morg, mprov, eff):
            return None
        # Named but absent here: perhaps a shared key (team/org) — we
        # hand over, never taking another account at this tier.
        return (key, eff) if key else None

    prets_vus = prets.PretsDuParcours()   # the lent rule along this walk

    def _group_fetch(gid: int, mprov: str):
        """TEAM fetch probe: same account selection as the member (Phase 2).
        Mono-account → the historical read (account=''); a loan, only NAMED (`prets`)."""
        # The CONTEXT org, not the group's: an override is read on the
        # requester (same seam as everywhere else).
        multi = cascade._is_multi_account(mprov, active_org)
        decide, hit = prets_vus.au_palier_equipe(gid, mprov, named_account if multi else None,
                                                 multi=multi)
        if decide:
            return hit
        if not multi:
            return group_store.get_group_secret(gid, mprov)
        eff, explicit = _pick_account("group", str(gid), mprov, "for your team",
                                      scope="group")
        key = prets_vus.cle_de_l_equipe(gid, mprov, eff)
        if eff and not key and not explicit:
            raise _not_found(eff, mprov)
        return (key, eff) if key else None

    def _org_fetch(oid: int, mprov: str):
        """ORG fetch probe: same account selection as the member (Phase 2).
        A NAMED account absent here hands over like the other tiers — it is
        the post-walk guard that raises (the walker may never reach this
        rung: context org None, non-org-shareable connector). A loan to the
        whole org, only NAMED (`prets`)."""
        multi = cascade._is_multi_account(mprov, oid)
        decide, hit = prets_vus.au_palier_org(oid, mprov, sub, named_account if multi else None,
                                              multi=multi)
        if decide:
            return hit
        if not multi:
            return org_store.get_org_secret(oid, mprov)
        eff, explicit = _pick_account("org", str(oid), mprov, "for your org",
                                      scope="org")
        key = org_store.get_org_secret(oid, mprov, eff)
        if eff and not key and not explicit:
            raise _not_found(eff, mprov)
        return (key, eff) if key else None

    def _tenant_fetch(slug: str, mprov: str):
        """TENANT fetch probe (L-keys PR 1): same account selection as the org.
        The walker only calls it for a sub of a third-party tenant (`rung_tenant`)."""
        if not cascade._is_multi_account(mprov, active_org):
            return tenant_vault.get_tenant_secret(slug, mprov)
        eff, explicit = _pick_account(credentials_store.TENANT, slug, mprov,
                                      "for your tenant")
        key = tenant_vault.get_tenant_secret(slug, mprov, eff)
        if eff and not key and not explicit:
            raise _not_found(eff, mprov)
        return (key, eff) if key else None

    # Single walk (only the winner is decrypted); `group` LAZY: resolved only if needed.
    probe = cascade.CascadeProbe(member=_member_fetch, member_cross=cascade.FETCH_PROBE.member_cross,
                         legacy_user=cascade.FETCH_PROBE.legacy_user,
                         group=_group_fetch, org=_org_fetch, tenant=_tenant_fetch,
                         platform=cascade.FETCH_PROBE.platform)
    # L7 (ADR 0053 blueprint) — a FLAG says who decides, the path not taken
    # computes alongside and is compared. Same probe, same downstream guards: only the
    # traversal changes. Detail and reversibility: `chain_shadow.barreau_gagnant`.
    win = chain_shadow.barreau_gagnant(
        provider, sub, active_org, probe=probe, want=want,
        group=lambda: scope.current_group(sub))

    prets_vus.refuser_si_seuls(win, porteur)
    # The org forbids personal keys here (#1239) and nothing shared serves: say so.
    if win is None or (named_account and win.mode == "platform"):
        cle_perso.refuser_si_interdite(active_org, porteur, cle_perso.A_L_APPEL)
    # Post-walk guard (review #399 F2): a NAMED account (param/axis/pin)
    # that won at NO keyed tier raises "not found" — never a PLATFORM key
    # silently (the platform tier has no accounts: answering there
    # under a different credential than the one requested would be an impersonation), never the
    # generic "no key" message. Covers the rungs the walker does not
    # reach: context org None, multi non-org-shareable connector (google,
    # browser, planity…).
    if named_account and (win is None or win.mode == "platform"):
        raise _not_found(named_account, provider)

    if win is None:
        lien_org = heritage.org_du_lien(sub, active_org)   # #480: their account, not the org
        # byo-only: no platform tier (basic_auth mounts, multi-secrets).
        if want == "byo":
            raise CredentialUnavailable(ErrorData(
                code=INVALID_PARAMS,
                message=(
                    indices.refus_sans_cle(
                        sub, active_org, provider,
                        f"No `{provider}` credential configured for you. ",
                        f"Set it{links.ou_poser_la_cle(sub, org=lien_org, connecteur=provider)}.")
                    + heritage.indice_refus(sub, active_org, provider)
                ),
            ))
        # The platform tier only exists if the registry ALLOWS it (gate in the walker).
        raise CredentialUnavailable(ErrorData(
            code=INVALID_PARAMS,
            # The key is set on the CARRIER (delegation): sending someone to
            # "the Whatsapp section" of their account page, where there is no field,
            # is a dead end. The "a team has the key" hint is also looked up
            # under the carrier — that is where shared secrets exist.
            # A team of the caller holding the key opens the refusal (#1317).
            message=(
                indices.refus_sans_cle(sub, active_org, porteur,
                                       f"No `{porteur}` key configured for you. ",
                                       indices._poser_ou_accorder(sub, lien_org, porteur))
                + heritage.indice_refus(sub, active_org, porteur)
            ),
        ))

    if check_usage and win.mode == "tenant":
        # Per-org budget of the tenant→org edge (L-keys PR 2) — no-op without an edge.
        # Read on the KEYS org (#480), like the walker: a beneficiary outside the
        # project's org does not draw on that org's budget.
        tenant_budget.enforce(win.entity_id, porteur,
                              heritage.org_des_cles(sub, active_org))
        if credits.rang_refacture(win.mode, win.entity_id) is not None:
            # The PRIMARY tenant's key, which the operator pays and re-bills like the
            # platform's: the units budget, after the edge's quota (a refusal there
            # leaves no hold).
            limite_du_jour.reserver(sub, heritage.org_des_cles(sub, active_org), "tenant")
    if win.mode != "platform":
        return ResolvedCredential(provider, win.payload, False, win.mode,
                                  *prets_vus.ligne_servie(win), account=win.account)
    if check_usage:  # paid option RE-READ on every use (ADR 0070 §7), no fallback
        quotas.exiger_option_payante(provider, sub, active_org)
        platform_rate.enforce(provider, sub, heritage.org_des_cles(sub, active_org),
                              hint=lambda: links.ou_poser_la_cle(sub, org=active_org))

    # ADR 0044 §F R3: the platform tier reads the PLATFORM-scope instances of the
    # unified vault (share_mode/share_down = access; meta.rate_limit* = quota).
    # The secret is only decrypted for the winning instance.
    # ⚠️ The PLATFORM grant carries the DECRYPTED secret. We do not unpack it into
    # a variable of this frame: the quota guards that follow may raise,
    # and a raising frame keeps its locals in the traceback. `win` is a
    # `CascadeRung`, whose `repr` is redacted (#564); a bare dict is not.
    # A connection configures the account: it does not consume a provider call.
    # Same key choice and same identity guards, without tenant debit or usage quota.
    used, limit = _win_quota(win, sub, provider, active_org) if check_usage else (0, 0)
    if limit and used >= limit:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Platform quota {provider} exceeded today ({used}/{limit}) "
                f"for key `{win.payload['label']}` — 0 remaining, the counter "
                f"resets at midnight. Set your own "
                f"key{links.ou_poser_la_cle(sub, org=active_org)} to "
                "lift the limit immediately."
            ),
        ))

    if limit and used + units > limit:
        # A batch debits `units` at once AFTER the call: checked against `used >= limit`
        # alone, it would pass with one unit remaining and exceed the shared key's
        # quota by `units - 1` (oto#168).
        raise quotas.refus_lot(provider, win.payload["label"], used, limit, units,
                               links.ou_poser_la_cle(sub, org=active_org))

    if check_usage:
        # The units budget: a tool of the perimeter reserves its worst case on the
        # operator's key, after every other guard — a refusal above leaves no hold.
        limite_du_jour.reserver(sub, heritage.org_des_cles(sub, active_org), "platform")
    return ResolvedCredential(provider, win.payload["secret"], True, "platform",
                              credentials_store.PLATFORM, win.payload["label"])


def _win_quota(win, sub: str, provider: str,
              active_org: Optional[int]) -> tuple[int, int]:
    """Today's (used, limit) for the winning PLATFORM edge `win`. `limit=0` =
    unlimited (registry without a default ceiling, OR quotas lifted by the person's
    `platform_unmetered` right in their org, ADR 0070 §7) — never a real ceiling of 0.

    SINGLE function for the pair: the refusal above, the read-only probe
    `platform_quota_hint` (below) and the displayed mode (`views.credential_mode_for`)
    all go through it. The ceiling itself comes from `quotas.plafond_du_jour`, which the
    `/api/me` snapshot also reads. The same number computed in two places ends up diverging —
    that is why the `cascade` walker exists (seen 2026-07-07, option rule copied 3×)."""
    used = quotas.platform_used_today(sub, provider, **quotas.compteurs(win.payload))
    limit = quotas.plafond_du_jour(win.payload, provider,
                                   lambda: quotas.quotas_leves(sub, active_org))
    return used, limit


def _resolve_pinned_instance(provider: str, sub: str, ref) -> ResolvedCredential:
    """HARD resolution of an explicit instance (`_instance=` OR project binding,
    ADR 0038 B6/B5): reads exactly the vault row the ref designates. ACCESS
    was guarded by `guard_instance_access` (at pin time for the axis; re-guarded for
    the CALLER on the binding path). Row absent = actionable McpError, NEVER a fallback to
    another tier (§C: acting under an identity other than the one requested is
    forbidden)."""
    from .. import instance_refs
    if ref.level == "member":
        # ⚠️ `ref.sub`, NOT the current `sub`: a `member` ref lent to a peer
        # (share_side, ADR 0044) carries the OWNER's identity, not the
        # borrower's — the borrower keeps their own org context elsewhere
        # (`guard_instance_access` co-sets that org), but the vault row to
        # READ remains the owner's. Bug found 2026-09-02: `sub`
        # here pointed to the borrower, so EVERY member-to-member loan failed
        # with "the instance no longer resolves" — a misleading message, the row
        # existed, it was just looked up under the wrong identity.
        etype, eid = credentials_store.MEMBER, credentials_store.member_id(ref.org_id, ref.sub)
        mode = "user"
        cle_perso.refuser_si_interdite(ref.org_id, provider, cle_perso.A_L_INSTANCE)  # #1239
    elif ref.level == "group":
        etype, eid, mode = "group", str(ref.group_id), "group"
    elif ref.level == "org":
        etype, eid, mode = "org", str(ref.org_id), "org"
    elif ref.level == "tenant":   # L-keys PR 1 — guarded by `guard_instance_access`
        etype, eid, mode = credentials_store.TENANT, ref.tenant, "tenant"
    else:  # platform — refused at pin time by the axis; defense in depth here.
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="`platform:` instance ref cannot be resolved via `_instance=` (B6)."))
    secret = credentials_store.get_credential(etype, eid, provider, ref.account)
    if not secret:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Instance `{instance_refs.format_ref(ref)}` no longer resolves "
                     "(credential removed or account renamed?). List again with "
                     "oto_instance(op='list') — no fallback to another identity.")))
    try:
        return ResolvedCredential(provider, secret, False, mode, etype, eid,
                                  account=ref.account)
    finally:
        # The decrypted secret does not stay bound in this frame (#564): an
        # exception passing through it afterwards would have nothing to pick up.
        del secret
