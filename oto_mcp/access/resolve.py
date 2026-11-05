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
from . import (cascade, chain_shadow, comptes, credits, indices, niveaux, platform_rate,
               quotas, rbac, resolve_anon, scope, tenant_budget)
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
    # `instance_id`: the account whose level the resolver judged (#1499).
    session_org.note_call_trace(instance=ref, instance_id=rc.instance_id,
                                resolved_connector=rc.provider,
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
    if pinned is not None and providers.credential_provider(pinned.connector or "") == porteur \
            and not (pinned.account or "").startswith("canal:"):   # hosted: ref_du_canal
        # The vault READ names the carrier, like the comparison above:
        # the pinned row is stored under it, and `require_credential` REFUSES a
        # delegator's name. Passing the bare name here raised a raw `ValueError` on
        # every channel call made under a pin — the pin was recognized then lost.
        return _resolve_pinned_instance(porteur, sub, pinned, provider)

    # PROJECT binding (ADR 0038 B5): the call's project (`_project=`) binds an
    # instance for this provider → HARD resolution, RE-GUARDED for the CALLER (the
    # binding was guarded for whoever set it; the caller of a shared project
    # may be another member). Explicit `_instance=` (above) takes precedence — the
    # most specific token of the call.
    bound = scope.project_pinned_instance(porteur)
    if bound is not None:
        rbac.guard_instance_access(sub, bound)
        return _resolve_pinned_instance(porteur, sub, bound, provider)

    # MEMBER scope (ADR 0033): "my key" exists ONLY in the context org —
    # set in org A, it does not resolve from org B. The org (`current_org` seam, ADR
    # 0023) is resolved BEFORE the first tier: no org-agnostic per-user credentials.
    active_org = scope.current_org(sub)

    # Account NAMED by the caller — explicit account (param) > call axis
    # `_account=` (#108) > project pin — resolved ONCE, before the walk:
    # it is looked for at every step, then among the accounts other persons share with
    # the caller (#1499); not found anywhere, it RAISES — never a fallback (#399 F2).
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

    # Who calls, what the call requires, which account of each step serves (#1499).
    choix = comptes.ChoixDuParcours(
        niveaux.Appelant(sub, active_org, lambda: scope.current_group(sub)),
        provider, niveaux.besoin())

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
        """The caller's OWN account in the call's org: she manages it, no level to judge.
        Several: the named one, else the only one, else her default (D3), else
        `ambiguous_account`. A SUSPENDED key is skipped: the next step serves (#401);
        NAMED, it is refused by name — another account than the named one never serves.
        Named: that account alone (absent, the step hands over); none listed: `''`."""
        multi = cascade._is_multi_account(mprov, morg)
        lignes = ([(named_account if multi else "", False)]
                  if named_account is not None or not multi else
                  [(a["account"], bool((a.get("meta") or {}).get("is_default")))
                   for a in credentials_store.list_accounts(
                       credentials_store.MEMBER, credentials_store.member_id(morg, msub),
                       mprov)] or [("", False)])
        actives = lignes if len(lignes) == 1 else [   # one: judged on its key, below
            (a, d) for a, d in lignes if not db.member_instance_suspended(msub, morg, mprov, a)]
        if not actives:   # all set aside: the next step serves, the echo says so
            return session_org.note_call_trace(cle_suspendue=True)
        if len(actives) > 1:
            tranche = [a for a, d in actives if d]
            if len(tranche) != 1:
                raise cascade.CompteAmbigu(ErrorData(
                    code=INVALID_PARAMS,
                    message=(f"Multiple {cascade.account_noun(mprov)}s `{mprov}` of your own ("
                             + ", ".join(f"`{a}`" for a, _ in actives)
                             + ") and no default — pass `_account=\"<name>\"`, or set a "
                             f"default with oto_identity(op='set', connector='{mprov}', "
                             "identity_id='<name>'). Nothing was done."),
                    data={"code": niveaux.CODE_COMPTE_AMBIGU, "retryable": False,
                          "accounts": [a for a, _ in actives]}))
            actives = [(tranche[0], True)]
        eff = actives[0][0]
        key = db.get_member_api_key(msub, morg, mprov, eff)
        if key and len(lignes) == 1 and db.member_instance_suspended(msub, morg, mprov, eff):
            if named_account is None:
                return session_org.note_call_trace(cle_suspendue=True)   # None: hand over
            raise _suspendu(mprov, eff)
        return (key, eff) if key else None

    def _suspendu(mprov: str, eff: str) -> McpError:
        return McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Your `{mprov}` key{' `' + eff + '`' if eff else ''}, which the call "
                     "names, is suspended: reactivate it (oto_instance op='resume'), or "
                     "name another account with `_account=`, or name none. Nothing was "
                     "done."),
            data={"code": niveaux.CODE_COMPTE_SUSPENDU, "retryable": False}))

    def _etape(entity_type: str, entity_id: str, mprov: str, ou: str, lire):
        """A TEAM or ORG step: the account its level serves; `lire` reads its key."""
        multi = cascade._is_multi_account(mprov, active_org)
        if multi and named_account is not None:
            lignes = [(named_account, False)] if lire(named_account) else []
        elif multi:   # none listed: the row `''`, as for the member
            lignes = [(a["account"], bool((a.get("meta") or {}).get("is_default")))
                      for a in credentials_store.list_accounts(entity_type, entity_id, mprov)
                      ] or ([("", False)] if lire("") else [])
        else:
            lignes = [("", False)] if lire("") else []
        compte = choix.a_l_etape(entity_type, entity_id, lignes,
                                 named_account if multi else None, ou=ou)
        key = lire(compte.account) if compte is not None else None
        return (key, compte.account) if key else None

    def _group_fetch(gid: int, mprov: str):
        return _etape("group", str(gid), mprov, "of your team",
                      lambda a: group_store.get_group_secret(gid, mprov, a))

    def _org_fetch(oid: int, mprov: str):
        return _etape("org", str(oid), mprov, "of your org",
                      lambda a: org_store.get_org_secret(oid, mprov, a))

    def _tenant_fetch(slug: str, mprov: str):
        """TENANT fetch probe (L-keys PR 1): unchanged by the account rules."""
        if not cascade._is_multi_account(mprov, active_org):
            return tenant_vault.get_tenant_secret(slug, mprov)
        eff = (named_account if named_account is not None
               else cascade._shared_auto_account(credentials_store.TENANT, slug, mprov,
                                                 "for your tenant"))
        key = tenant_vault.get_tenant_secret(slug, mprov, eff)
        if eff and not key and named_account is None:
            raise _not_found(eff, mprov)
        return (key, eff) if key else None

    # Single walk (only the winner is decrypted); `group` LAZY: resolved only if needed.
    probe = cascade.CascadeProbe(member=_member_fetch,
                                 legacy_user=cascade.FETCH_PROBE.legacy_user,
                                 group=_group_fetch, org=_org_fetch, tenant=_tenant_fetch,
                                 platform=cascade.FETCH_PROBE.platform)
    # L7 (ADR 0053 blueprint) — a FLAG says who decides, the path not taken
    # computes alongside and is compared. Same probe, same downstream guards: only the
    # traversal changes. Detail and reversibility: `chain_shadow.barreau_gagnant`.
    win = chain_shadow.barreau_gagnant(
        provider, sub, active_org, probe=probe, want=want,
        group=lambda: choix.appelant.equipe_active)

    # A NAMED account no keyed step holds: another person's of the org, else one of
    # another org (#172, D1) — at the level its rules give, never a platform key instead.
    if named_account and (win is None or win.mode == "platform"):
        autrui = choix.autrui_nomme(named_account)
        key = autrui and credentials_store.get_credential(
            autrui.owner_type, autrui.owner_id, porteur, autrui.account)
        if key:
            return ResolvedCredential(provider, key, False, "user", autrui.owner_type,
                                      autrui.owner_id, account=autrui.account,
                                      instance_id=autrui.instance_id)
        raise _not_found(named_account, provider)
    choix.refuser_si_sous_le_niveau(win)
    # STEP 5 (#1499): nothing local, nothing named — an account lent to her, or her key
    # elsewhere, before the tenant's and platform's key (`ChoixDuParcours.cle_d_ailleurs`).
    servi = (choix.cle_d_ailleurs(lambda: cle_perso.interdite(active_org, porteur))
             if not named_account and (win is None or win.mode in ("tenant", "platform"))
             else None)
    if servi is not None:
        c, key = servi
        return ResolvedCredential(provider, key, False, "user", c.owner_type, c.owner_id,
                                  account=c.account, instance_id=c.instance_id)
    if win is None:   # personal keys forbidden (#1239), else those she could NAME
        cle_perso.refuser_si_interdite(active_org, porteur, cle_perso.A_L_APPEL)
        a_nommer = choix.a_nommer()
        en_plus = niveaux.phrase_des_candidats(a_nommer)
        # byo-only: no platform tier (basic_auth mounts, multi-secrets).
        if want == "byo":
            raise CredentialUnavailable(ErrorData(
                code=INVALID_PARAMS,
                message=indices.refus_sans_cle(
                    sub, active_org, provider,
                    f"No `{provider}` credential configured for you. ",
                    f"Set it{links.ou_poser_la_cle(sub, org=active_org, connecteur=provider)}."
                ) + en_plus,
                data={"to_name": a_nommer} if a_nommer else None,
            ))
        # The platform tier only exists if the registry ALLOWS it (gate in the walker).
        raise CredentialUnavailable(ErrorData(
            code=INVALID_PARAMS,
            # The key is set on the CARRIER (delegation); a team of the caller holding the
            # key opens the refusal (#1317).
            message=indices.refus_sans_cle(sub, active_org, porteur,
                                           f"No `{porteur}` key configured for you. ",
                                           indices._poser_ou_accorder(sub, active_org,
                                                                      porteur)) + en_plus,
            data={"to_name": a_nommer} if a_nommer else None,
        ))

    # The org that PAYS for the call: the call's org, for a member of it — a beneficiary
    # of a project of another org draws on none of that org's budgets (#1499, D1).
    org_m = scope.org_d_appartenance(sub, active_org)
    if check_usage and win.mode == "tenant":
        # Per-org budget of the tenant→org edge (L-keys PR 2) — no-op without an edge.
        tenant_budget.enforce(win.entity_id, porteur, org_m)
        if credits.rang_refacture(win.mode, win.entity_id) is not None:
            # The PRIMARY tenant's key, which the operator pays and re-bills like the
            # platform's: the units budget, after the edge's quota (a refusal there
            # leaves no hold).
            credits.reserver(sub, org_m, "tenant")
    if win.mode != "platform":
        servi = choix.servis.get((win.entity_type, win.entity_id, win.account or ""))
        return ResolvedCredential(provider, win.payload, False, win.mode, win.entity_type,
                                  win.entity_id, account=win.account,
                                  instance_id=servi.instance_id if servi else None)
    if check_usage:  # paid option RE-READ on every use (ADR 0070 §7), no fallback
        quotas.exiger_option_payante(provider, sub, active_org)
        platform_rate.enforce(provider, sub, org_m,
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
        credits.reserver(sub, org_m, "platform")
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
    used = quotas.platform_used_today(sub, provider, win.payload.get("edge_counter"))
    limit = quotas.plafond_du_jour(win.payload, provider,
                                   lambda: quotas.quotas_leves(sub, active_org))
    return used, limit


def _plus_resolue(ref) -> McpError:
    return McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Instance `{instance_refs.format_ref(ref)}` no longer resolves "
                 "(credential removed or account renamed?). List again with "
                 "oto_instance(op='list') — no fallback to another identity.")))


def _resolve_pinned_instance(provider: str, sub: str, ref,
                             service: Optional[str] = None) -> ResolvedCredential:
    """HARD resolution of an explicit instance (`_instance=` OR project binding, ADR 0038
    B6/B5): exactly the vault row the ref designates. ACCESS was guarded by
    `guard_instance_access`; the LEVEL the call requires is judged here, on that account
    (#1499). Row absent = actionable McpError, NEVER another tier (§C)."""
    instance_id = None
    if ref.level == "member":
        # `ref.sub`, NOT the current `sub`: another person's account carries its OWNER's
        # identity; the caller keeps her own org context.
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
    if mode != "tenant":
        choix = comptes.ChoixDuParcours(
            niveaux.Appelant(sub, scope.current_org(sub), lambda: scope.current_group(sub)),
            service or provider, niveaux.besoin())
        compte = comptes.comptes_des_lignes(provider, [(etype, eid, ref.account or "")]).get(
            (etype, eid, ref.account or ""))
        if compte is None:
            raise _plus_resolue(ref)
        if not (mode == "user" and ref.sub == sub):
            choix.exiger(compte)
        choix.servir(compte)    # an account of another org: the echo says so (#1499)
        instance_id = compte.instance_id
    secret = credentials_store.get_credential(etype, eid, provider, ref.account)
    if not secret:
        raise _plus_resolue(ref)
    try:
        return ResolvedCredential(provider, secret, False, mode, etype, eid,
                                  account=ref.account, instance_id=instance_id)
    finally:
        # The decrypted secret does not stay bound in this frame (#564): an
        # exception passing through it afterwards would have nothing to pick up.
        del secret
