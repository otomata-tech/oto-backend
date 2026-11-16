"""The PLATFORM rung of the cascade (ADR 0044 §F, blueprint ADR 0053 lot L5).

Extracted from `cascade.py` (17/09, `status_for` N+1 batch): this seam — which, from a
platform instance, designates the beneficiary, computes its quota, and arbitrates
between the grants chain and the old path — depends only on `scope`
(membership in a sharing scope) and on `grants_chain`. The walker
(`walk_cascade`) calls it via the `platform` rung of a `CascadeProbe`,
never directly.
"""
from __future__ import annotations

from .. import credentials_store, grants_chain, providers
from . import scope


def _platform_grantee_scope(sub, active_org, scopes) -> "str | None":
    """The scope in `scopes` that targets `sub` on a PLATFORM instance, or None (ADR 0044
    §F). `user:<sub>` wins (the most specific); then the **ACTIVE** team `group:<gid>`
    (#1239, decision of 13/10/2026), only if `sub` is one of its members in the active
    org; then `org:<id>` gated on the **ACTIVE** org (EXACT mirror of the old
    `get_active_org_grant(active_org)` — an org grant is metered per org context, not per
    membership: a member of org X active in Y does not benefit from it). Serves access
    (closed) AND quota (rate_limit_by). EXACT mirror of `grants_chain.grantee_scopes`; the
    team is only looked up when a `group:` scope is listed (the old path pays nothing)."""
    if not scopes:
        return None
    if f"user:{sub}" in scopes:
        return f"user:{sub}"
    if any(str(s).startswith("group:") for s in scopes):
        g = grants_chain.active_group(sub, active_org)
        if g is not None and f"group:{g}" in scopes:
            return f"group:{g}"
    if active_org is not None and f"org:{active_org}" in scopes:
        return f"org:{active_org}"
    return None


#: The meta key that carries the REVOKED scopes of a platform instance (F-046). Written
#: by `credentials_store.platform_revoke`, cleared by `platform_grant`; read here only.
DENIED_META_KEY = "share_denied"


def _platform_denied(sub, active_org, inst: dict) -> bool:
    """Has `sub` (or its active org) been REVOKED on this platform instance? (F-046)

    A revocation used to be "remove from `share_down`" and nothing else — on an OPEN key
    with an empty `share_down` (open to all), that removed nothing, and the next call was
    served exactly as before: a credit block that did not block. The revoked scope is
    now remembered in `meta.share_denied`, and a denial wins over everything: the open
    tier, a `share_down` grant of the other scope, a `share_side` loan. Any matching
    scope refuses — `user:<sub>`, the active team `group:<gid>` (#1239) or
    `org:<active org>` — because a block is aimed at one of them, and the others must
    not reopen it."""
    denied = (inst.get("meta") or {}).get(DENIED_META_KEY) or []
    if not denied:
        return False
    if sub and f"user:{sub}" in denied:
        return True
    if any(str(s).startswith("group:") for s in denied):
        g = grants_chain.active_group(sub, active_org)
        if g is not None and f"group:{g}" in denied:
            return True
    return active_org is not None and f"org:{active_org}" in denied


def _platform_instance_usable(sub, active_org, inst: dict) -> bool:
    """Is the platform instance usable by `sub`? (ADR 0044 §F, mode-aware). A REVOKED
    scope (`meta.share_denied`, F-046) refuses first, whatever follows. A `share_side`
    loan authorizes (membership, like a BYO loan). Otherwise per `share_mode`:
    'open' = empty `share_down` (free tier, open to all) OR `sub` is a grantee; 'closed' =
    `sub` is a grantee (closed by default)."""
    if _platform_denied(sub, active_org, inst):
        return False
    down, side = inst.get("share_down") or [], inst.get("share_side") or []
    if scope._sub_matches_scopes(sub, side):
        return True
    granted = _platform_grantee_scope(sub, active_org, down) is not None
    if inst.get("share_mode") == "closed":
        return bool(down) and granted
    return (not down) or granted


def _platform_quota(sub, active_org, meta: dict) -> "int | None":
    """Daily quota of the beneficiary on a platform instance: `rate_limit_by[sub's scope]`
    (user wins > active org), otherwise the instance's default `rate_limit`."""
    rlb = (meta or {}).get("rate_limit_by") or {}
    sc = _platform_grantee_scope(sub, active_org, list(rlb.keys()))
    if sc is not None and sc in rlb:
        return rlb[sc]
    return (meta or {}).get("rate_limit")


def _legacy_platform_grant_meta(sub, provider, active_org, *,
                                instances: "list[dict] | None" = None) -> "dict | None":
    """Platform rung (ADR 0044 §F R3) WITHOUT a secret: {label, daily_quota} of the most
    recent PLATFORM instance usable by `sub`, or None. Basis of the `status_for`/
    `credential_mode_for` mirrors (presence + quota, never decryption).

    ⚠️ **The old path, and it does not move by a single byte** (blueprint ADR 0053, lot L5):
    it remains the only one for the nine non-switched connectors, and the EXACT fallback for a
    beneficiary the chain does not know (the chain is SILENT). It is no longer read where
    the chain has an opinion (R6).

    `instances`: read in advance (cf. `cascade.preloaded_presence_probe`) — `None` rereads.

    A revocation holds for the CONNECTOR, not for one of its keys: as soon as one
    instance denies the caller, none serves it. Otherwise an older key open to all, or
    a key set after the revocation, served the revoked caller again."""
    insts = (instances if instances is not None
             else credentials_store.list_platform_instances(provider))
    if any(_platform_denied(sub, active_org, inst) for inst in insts):
        return None
    for inst in insts:
        if _platform_instance_usable(sub, active_org, inst):
            return {"label": inst["label"],
                    "daily_quota": _platform_quota(sub, active_org, inst.get("meta"))}
    return None


def _platform_grant_meta(sub, provider, active_org, *,
                         instances: "list[dict] | None" = None) -> "dict | None":
    """The platform rung, **grants chain first** (blueprint ADR 0053, lot L5).

    Three outcomes, and the third is what keeps the switch safe:

    - the chain GRANTS → its verdict (key + quota carried by the edge);
    - the chain REFUSES (edges exist, all revoked) → refusal **without fallback**:
      otherwise revoking an edge would cut nothing, the old free-tier path
      immediately re-granting;
    - the chain is SILENT (connector not switched, or no edge ever targeted this
      caller) → the old path, unchanged.

    Where the chain has an opinion, the old path is no longer read (R6,
    `docs/credits-au-moment-de-l-appel.md`): the double-read window and its discrepancy
    journal are closed. A `user` edge also carries `edge_counter` — the edge whose
    counter the refusal reads (`quotas.platform_used_today`); under an `org` or `group`
    edge, the refusal keeps reading `usage`, counted per person.

    For a connector whose quota counts per org (`Connector.grant_quota_per == "org"`),
    the meta also carries `org_counter`: the org whose counter the refusal reads — the
    org the call is made in, whichever scope granted (the settlement debits the same
    one, `quotas.debiter_plateforme`). Without an org, such a key is not served: there
    is no counter to hold its quota.

    `instances`: passed as is to whichever path answers."""
    verdict = grants_chain.platform_rung(sub, provider, active_org, instances=instances)
    if verdict is None:
        meta = _legacy_platform_grant_meta(sub, provider, active_org, instances=instances)
    elif not verdict.granted:
        return None
    else:
        meta = {"label": verdict.label, "daily_quota": verdict.quota}
        if verdict.grantee is not None and verdict.grantee[0] == "user":
            meta["edge_counter"] = verdict.resource_id
    if meta is None or not quota_per_org(provider):
        return meta
    if active_org is None:
        return None
    return {**meta, "org_counter": int(active_org)}


def quota_per_org(provider: str) -> bool:
    """Does the platform key of `provider` (normalized to its credential carrier) count
    its daily quota per ORG rather than per person? (`Connector.grant_quota_per`)"""
    con = providers.REGISTRY.get(providers.credential_provider(provider))
    return con is not None and con.grant_quota_per == "org"


def _resolve_platform_grant(sub, provider, active_org) -> "dict | None":
    """Platform rung WITH a secret: {label, secret, daily_quota} or None. Replaces the 3
    legacy reads (get_active_grant/get_active_org_grant/get_platform_api_key). The secret
    is decrypted ONLY for the winning instance (hot path)."""
    g = _platform_grant_meta(sub, provider, active_org)
    if not g:
        return None
    secret = credentials_store.get_credential(credentials_store.PLATFORM, g["label"], provider)
    if secret is None:
        return None
    return {**g, "secret": secret}
