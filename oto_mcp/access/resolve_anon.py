"""Resolution for an ANONYMOUS MCP endpoint (ADR 0032) — the org-only mirror.

Extracted from `resolve.py` on 2026-08-29 (500-line ratchet, #584): it is a path in
its own right, with its ADR, its callers (`subdomain_project`) and its tests — not
a branch of the identified path. It shares the walker and the return type, nothing
else.

⚠️ **The tenant tier, here, ONLY comes from an edge** (L-keys PR 2). A caller's
tenant is read from their qualified sub, and the anonymous caller has none; reading the
project org's attachment is precisely what a resolution path does not do (lot
L1). It is the walker that looks for the live tenant→org edge (`grants_chain.
tenant_for_org`); without it, the cascade stays `org > platform`.
"""
from __future__ import annotations

from typing import Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import credentials_store, org_store, providers
from . import cascade, platform_rate, quotas, tenant_budget
from .resolved_credential import ResolvedCredential


def _resolve_credential_anon(provider: str, want: str, org_id: Optional[int],
                             check_usage: bool = True) -> ResolvedCredential:
    """Resolution for an ANONYMOUS MCP endpoint (ADR 0032): no `sub`, no per-user
    session → reduced cascade `org_secret > org platform grant > open platform
    key`, scoped to the project's OWNER org. No user_key/group (nonexistent
    without an identity), no per-sub quota (the subdomain's rate limit bounds abuse).
    Org-only mirror of the `_resolve_credential_impl` rungs — whatever is not resolvable
    at the org level (per-user oauth/cookie) raises an actionable McpError, fail-closed."""
    con = providers.connector_for_provider(provider)
    if con is None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown provider: {provider}"))
    if org_id is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"The anonymous endpoint has no owner org to resolve "
                     f"`{provider}` (project without an org).")))
    # Walker with sub=None: the member/group/tenant rungs skip themselves
    # → reduced cascade org > platform (ADR 0044 §F R3: anon → 'open' free-tier
    # instance, or 'closed' whose share_down targets `org:<org_id>`).
    # ⚠️ The org rung selects its ACCOUNT like the real path (`_org_fetch`:
    # unique/`is_default`), never a hardcoded `''` — `ensure_named_coexistence` migrates the
    # mono row to "principal" at the first named account, and the anonymous endpoint
    # then stopped resolving while `has_org_secret` said "configured"
    # (review #399 F3). No nameable account here: no sub, no call axis.
    def _anon_org_fetch(oid: int, mprov: str):
        # Mono FIRST: the historical `''` row answers without reading the accounts
        # table (zero added cost for pre-migration orgs, and the contract of the
        # tests that stub `get_org_secret` alone stays intact). Named selection
        # is attempted ONLY if the mono row is missing — the F3 case, where
        # `ensure_named_coexistence` migrated it to "principal".
        key = org_store.get_org_secret(oid, mprov)
        if key or not cascade._is_multi_account(mprov, oid):
            return key
        eff = cascade._shared_auto_account("org", str(oid), mprov,
                                   "for this project's org", scope="org")
        if not eff:
            return None
        key = org_store.get_org_secret(oid, mprov, eff)
        return (key, eff) if key else None

    # `legacy_user` is never reached here (the rung is gated on `sub is not
    # None`, and the anonymous caller has none) — the real probe is passed anyway,
    # a required field of `CascadeProbe` (#409: a probe that omitted it would
    # skip it silently if this rung ever became reachable here).
    probe = cascade.CascadeProbe(member=cascade.FETCH_PROBE.member,
                         legacy_user=cascade.FETCH_PROBE.legacy_user,
                         group=cascade.FETCH_PROBE.group, org=_anon_org_fetch,
                         tenant=cascade.FETCH_PROBE.tenant,
                         platform=cascade.FETCH_PROBE.platform)
    win = cascade.cascade_winner(None, provider, org=org_id, group=None,
                         probe=probe, want=want)
    if win is None:
        if want == "byo":
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"No `{provider}` credential configured for this project's org."))
        # Read by a visitor WITHOUT an account (a published project): telling them to
        # set a key is advice they cannot follow. Say who must act, and that the
        # visitor has nothing to do.
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"This published project cannot use `{provider}`: its owner's org "
                     f"has no `{provider}` key and no access to a shared one. Nothing "
                     f"to do on the visitor's side — the project owner must set an org "
                     f"key, or ask oto's admins to grant their org access.")))
    if win.mode == "org":
        return ResolvedCredential(provider, win.payload, False, "org", "org",
                                  str(org_id), account=win.account)
    if win.mode == "tenant":
        # Served by a live edge (never otherwise for the anonymous caller): its per-org
        # budget applies — the whole org draws from it, anonymous included.
        tenant_budget.enforce(win.entity_id, providers.credential_provider(provider), org_id)
        return ResolvedCredential(provider, win.payload, False, "tenant",
                                  credentials_store.TENANT, win.entity_id,
                                  account=win.account)
    # The paid option is reread on each use, as on the identified path (ADR 0070 §7):
    # the project's owner org must hold the live entitlement.
    if check_usage:
        quotas.exiger_option_payante(provider, None, org_id)
        # Per-org fairness on the shared key (F-051): the anonymous visitors of a
        # published project draw on their owner org's share, like its members.
        platform_rate.enforce(provider, None, org_id)
    # Same rule as at the platform rung of the identified path: the secret stays
    # in the `CascadeRung` (redacted repr), never in a bare dict of this frame.
    return ResolvedCredential(provider, win.payload["secret"], True, "platform",
                              credentials_store.PLATFORM, win.payload["label"])
