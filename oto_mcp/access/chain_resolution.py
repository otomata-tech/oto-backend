"""What the grants chain POINTS TO — the 0053 resolution, isolated (batch L7).

A read that is impossible RAISES: it never means "key absent, try further
down". Only the comparison observer may absorb an error from this chain.

**Why this module exists separately.** It is the half of the batch that SURVIVES: when
`walk_cascade` is removed (PR 3), the observation and its counter disappear, but
this stays — it is the served resolution. Keeping them in the same file would have mixed
what we install with what we throw away.

**What it computes**, as [0053-D2](blueprint) lays it out:

1. the **reachable set** — the instances of the scopes the subject is a MEMBER of,
   plus those that come down to them through a live `grants` edge;
2. the **designation** — the call that names an instance and the procedure binding
   take precedence, but they already short-circuit the walk upstream (`resolve`), so
   what remains here is **proximity**: `user > group > org > platform`.

There is no restriction on top: 0053-D1 — restricting means PLACING
ownership at the right level, never laying a prohibition on top (the `connector_acl`
table has not been read since 2026-09-24). The org's personal-key policy (#1239,
`connectors.cle_perso`) is not one either: it names nobody, it removes the MEMBER tier
from the traversal for everyone in the org, read at its source like the other notches.

**Two method rules, held mechanically:**

1. **No rule is copied.** The connector's notches are read at their SOURCE —
   the registry (`is_byo_user`, `org_shareable`, `auth_modes`), an instance's
   suspension, the `grants` edges. This module writes a different TRAVERSAL, not
   a second copy of the gates. Same discipline as
   the derived visibility (R9), which inverted the walker without cloning it.
2. **The designation concerns the TIER, not the account.** The choice of a
   multi-identity account is a notch of the instance (0053-D9), not an authorization:
   `rung_for_pick` delegates it to the PROBE that `resolve` composed, and never
   replays it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .. import (credentials_store, grants_chain, group_store, org_store, providers,
                tenant_vault)
from ..connectors import cle_perso
from ..db import grants as db_grants
from . import scope

logger = logging.getLogger(__name__)

# The two NUANCES of the hole — "the vault grants, the chain cannot say so".
# They live here because it is the resolution that OBSERVES them; `chain_shadow` takes
# them over as they are in its class vocabulary, without redeclaring them (a
# served value declared twice ends up diverging).
FREE_TIER_HORS_MODELE = "free_tier_hors_modele"
PARTAGE_HORS_MODELE = "partage_hors_modele"

# ── The reachable set, and its designation ────────────────────────────────────

@dataclass(frozen=True)
class ChainPick:
    """What the chain WOULD DESIGNATE. `mode` speaks the same vocabulary as
    `CascadeRung.mode`, so that the comparison is an equality and not a
    translation. `via` says WHY the instance is reachable — membership of the
    owning scope (D1, first clause) or a grant edge (second)."""
    mode: str                       # user | group | org | tenant | platform
    entity_type: Optional[str]
    entity_id: Optional[str]
    via: str = "appartenance"       # appartenance (membership) | grant
    group_id: Optional[int] = None


def _group_ids(sub: str, org: Optional[int]) -> list[int]:
    """All of the subject's teams in the context org — **all** of them, not the active one.
    This is where 0053-D2 widens, and the widening is the subject of the measurement."""
    if org is None:
        return []
    return sorted(int(g["group_id"]) for g in group_store.list_groups_for_user(sub, org))


def _platform_pick(sub: str, provider: str, org: Optional[int]) -> "tuple[Optional[ChainPick], Optional[str]]":
    """The platform tier seen by the CHAIN ALONE, and nothing else.

    Returns `(pick, hors_modele)`. Unlike `grants_chain.platform_rung`,
    no `CHAIN_CONNECTORS` gate: L7 makes the chain the sole authority, so the
    question "what about a connector that has not been switched over?" is precisely the one
    we measure. Edges are read by the SAME function as the served path
    (`db_grants.edges_for`) — not a copied query.

    **`hors_modele` names the NUANCE of the hole**, and it is no longer a boolean. A vault
    row can grant in two ways the chain cannot yet express, and
    they have neither the same remedy nor the same reading:

    - **open to all** (`share_mode='open'`, no allowlist) ⟹ the "everyone" edge
      is missing;
    - **closed on an allowlist** (`share_down`) ⟹ the NAMED edges of that
      allowlist are missing. L5's seeding only covered `CHAIN_CONNECTORS`, so
      every closed key outside that list falls in this case.

    Distinguishing them is not a refinement: without the second, a perfectly
    explainable divergence fell into `inconnu` — the class that must stay at zero
    to allow the removal — and closed the door for a wrong reason. Seen on
    2026-08-29: 17 observations on `aiark` and `apify`, two CLOSED keys granted
    to an org, without a single edge.

    The shape is read from the vault, at its source, without replaying the access rule of
    the old path: we look at what the instance IS, not who it authorizes."""
    nominatifs = grants_chain.grantee_scopes(sub, org)
    hors_modele = None
    par_cle = []
    for inst in credentials_store.list_platform_instances(provider):
        ref = grants_chain.instance_ref(inst["label"], provider)
        par_cle.append((inst, db_grants.edges_for(ref, nominatifs + [grants_chain.EVERYONE])))
    # The revocation wins over any grant, whatever the grain and whichever key carries
    # it (same rule as `grants_chain.platform_rung`): a scope of the caller revoked on
    # the connector refuses, even with a live edge on the other scope or an "everyone"
    # edge on a key set after the revocation. Refused, not a hole of the model.
    if grants_chain.revoked_scopes([e for _, edges in par_cle for e in edges], nominatifs):
        return (None, None)
    for inst, edges in par_cle:
        nomme = [e for e in edges
                 if (e["grantee_kind"], e["grantee_id"]) != grants_chain.EVERYONE]
        tous = [e for e in edges
                if (e["grantee_kind"], e["grantee_id"]) == grants_chain.EVERYONE
                and e.get("revoked_at") is None]
        if not edges:
            # Nothing to say about THIS instance: we note which nuance of hole it
            # would be if the old path did grant. The first one met
            # wins — same order as the old path (most recent first).
            if hors_modele is None:
                ouverte = (inst.get("share_mode") != "closed"
                           and not (inst.get("share_down") or []))
                hors_modele = (FREE_TIER_HORS_MODELE if ouverte
                               else PARTAGE_HORS_MODELE)
            continue
        # An edge that NAMES the caller takes precedence over "everyone". Revoked
        # named edges were handled above (connector-wide refusal); one archived here
        # was REPLACED by a live edge of the same scope, possibly on another key.
        if any(e.get("revoked_at") is None for e in nomme):
            return (ChainPick("platform", credentials_store.PLATFORM,
                              inst["label"], via="grant"), hors_modele)
        if tous:
            return (ChainPick("platform", credentials_store.PLATFORM, inst["label"],
                              via="tout_le_monde"), hors_modele)
    return (None, hors_modele)


def _paliers(sub: str, provider: str, org: Optional[int], want: str,
             *, group=scope._UNSET):
    """The REACHABLE tiers, in order, as a **generator** — and the nuance of the
    hole as the return value (PEP 380, read through `StopIteration.value`).

    ⚠️ **Generator and not list, and this is not a style detail.** A list
    would probe ALL tiers on every call — the member, each team, the org, the
    tenant, plus the platform instances — on the product's hottest path,
    for a result of which almost always only the first element is consumed. The
    generator keeps the nominal cost identical to today's: the next tier is only
    probed if the previous one returned nothing.

    ⚠️ **And this is not an ADDED shape: it is the one the traversal had
    lost.** The historical path (`walk_cascade`) is a generator — a named account
    missing at the member tier passed the hand to the org tier, a contract written in
    its code. The chain replaced it with a `return` at the first tier that HOLDS a
    key, and the read resolves at fetch: a miss became a flat refusal instead of
    a fallback (#673). Yielding instead of returning restores the fallback, without rewriting anything in
    the read.

    The connector's notches (byo_user, org-shareable, declared platform tier,
    suspended instance) are read at their source — they are properties of the
    instance, not authorizations, and they hold on both sides of the window.
    """
    porteur = providers.credential_provider(provider)
    # The org of the call whose member the subject is — None for the beneficiary of a
    # shared project of another org (#1499, D1): same seam as the walker.
    org_cles = scope.org_d_appartenance(sub, org)
    if org is not None and providers.is_byo_user(porteur):
        if (credentials_store.has_credential(
                credentials_store.MEMBER, credentials_store.member_id(org, sub),
                porteur, account=None)
                # A SUSPENDED key is skipped, like the walker's: the next rung serves.
                and not credentials_store.instance_suspended(
                    credentials_store.MEMBER, credentials_store.member_id(org, sub), porteur)
                and not cle_perso.interdite(org, porteur)):   # the org's policy (#1239)
            # The free-tier flag is only used if the chain stays silent.
            yield ChainPick("user", credentials_store.MEMBER,
                            credentials_store.member_id(org, sub))
    if porteur in providers.ORG_SHAREABLE_PROVIDERS:
        # At equal proximity, the ACTIVE team first — it is the most
        # favourable path in the sense of D5, and it makes the designation deterministic when the
        # subject belongs to several teams that all hold a key.
        active = scope.current_group(sub) if group is scope._UNSET else group
        active = active() if callable(active) else active
        gids = _group_ids(sub, org)
        if active is not None and int(active) in gids:
            gids = [int(active)] + [g for g in gids if g != int(active)]
        for gid in gids:
            if group_store.has_group_secret(gid, porteur):
                yield ChainPick("group", "group", str(gid), group_id=gid)
        if org_cles is not None:
            if org_store.has_org_secret(org_cles, porteur):
                yield ChainPick("org", "org", str(org_cles))
        # TENANT tier (L-keys PR 1): the same as in the walker, read from the same source
        # (`rung_tenant` — the qualified sub, never the org). Without it, every served tenant
        # key would count an `inconnu` divergence that this batch would have created.
        slug = tenant_vault.rung_tenant(sub)
        if slug is not None:
            if (credentials_store.has_credential(credentials_store.TENANT, slug, porteur)
                    and not credentials_store.instance_suspended(
                        credentials_store.TENANT, slug, porteur)):
                # Same edge as the walker: SILENT ⟹ membership; REFUSES ⟹ move on.
                verdict = grants_chain.tenant_rung(slug, porteur, org_cles)
                if verdict is None or verdict.granted:
                    yield ChainPick("tenant", credentials_store.TENANT, slug,
                                    via="grant" if verdict else "appartenance")
    if want != "byo":
        con = providers.connector_for_provider(porteur)
        if con is not None and "platform" in con.auth_modes:
            pick, hors_modele = _platform_pick(sub, porteur, org_cles)
            if pick is not None:
                yield pick
            # The nuance is only computed if the chain stays silent — the `_platform_pick`
            # above is the only pass that reads the platform instances, and it comes
            # out of it. Returning it here keeps it attached to its pass: taking it out of the
            # generator would require a second read on the busiest
            # connector (an open key IS the case where the chain stays silent).
            return hors_modele
    return None


def chain_verdict(sub: str, provider: str, *, org: Optional[int],
                  want: str = "auto") -> "tuple[Optional[ChainPick], Optional[str]]":
    """The instance that 0053-D2 WOULD DESIGNATE, **and** the nuance of the hole if it is silent.

    The designation stays what it was: the FIRST reachable tier. What the
    traversal regained (#673) serves the READ, not the comparison — the window
    report compares designations, and handing it a list would shift what it
    measures at the moment we fix something else.

    So only one element is consumed: the following tiers are never probed if
    the first one answers. The nuance is the RETURN value of the generator — it
    only exists when it is exhausted without yielding anything, that is exactly when the
    chain is silent.
    """
    paliers = _paliers(sub, provider, org, want)
    try:
        return (next(paliers), None)
    except StopIteration as fin:
        return (None, fin.value)


def chain_paliers(sub: str, provider: str, *, org: Optional[int],
                  want: str = "auto", group=scope._UNSET):
    """The reachable tiers IN ORDER — what the read walks through.

    This is the surface `rung_for_picks` consumes: it stops at the first tier
    that ANSWERS, where `chain_verdict` stops at the first one that EXISTS. The difference
    between the two is the whole subject of #673.
    """
    return _paliers(sub, provider, org, want, group=group)


def chain_winner(sub: str, provider: str, *, org: Optional[int],
                 want: str = "auto") -> Optional[ChainPick]:
    """`chain_verdict` without its flag — the view that gets read, and the one PR 2
    will promote to the served resolution."""
    return chain_verdict(sub, provider, org=org, want=want)[0]



def rung_for_picks(paliers, probe, sub: str, provider: str, org: Optional[int]):
    """The first tier that ANSWERS, walking through the reachable tiers.

    ⚠️ **This is the fallback, and it was dead.** The chain designates a tier on the
    PRESENCE of a credential (`has_credential(account=None)` — any account);
    the read resolves at FETCH, with named-account selection. The two therefore do not
    always answer the same thing: a named account missing at the member tier
    exists "in presence" and is missing "at fetch". The historical path then
    passed the hand to the next tier — "the org had it, the member didn't", a contract
    written in its code. Designating ONE tier and then returning `None` on a miss turned this
    fallback into a **flat refusal** (#673).

    So we walk through, and the cost stays what it was: the generator only probes the
    next tier if the previous one returned nothing, and the nominal case stops at the
    first.
    """
    return next(rungs_for_picks(paliers, probe, sub, provider, org), None)


def rungs_for_picks(paliers, probe, sub: str, provider: str, org: Optional[int]):
    """The probe's answers, without consuming the following tiers in advance."""
    for pick in paliers:
        rung = rung_for_pick(pick, probe, sub, provider, org)
        if rung is not None:
            yield rung


def rung_for_pick(pick: Optional[ChainPick], probe, sub: str, provider: str,
                  org: Optional[int]):
    """The SERVED rung corresponding to the chain's designation, or None.

    **We do not rewrite the FETCH, we reuse the probes.** The walker did two
    things: traverse (the order of the rungs, the gates) and read (the probe, with its
    multi-identity account selection, its suspension, its decryption of the winner
    only). L7 only replaces the **traversal**; the read stays the probe that
    `resolve` already composed. This is what makes inverting the authority replay
    no account rule — hence makes none of them diverge.

    Returns a `cascade.CascadeRung`, the same shape as what `cascade_winner` returned:
    everything that follows in `resolve` (named-account guard, quota, `ResolvedCredential`)
    is then unchanged, line for line."""
    if pick is None:
        return None
    from . import cascade  # late import: `cascade` is a sibling, not a dependency
    if pick.mode == "user":
        hit = probe.member(sub, org, provider)
        if hit is None:
            return None
        payload, account = hit
        return cascade.CascadeRung("user", pick.entity_type, pick.entity_id, payload,
                                   account)
    if pick.mode == "group":
        hit = probe.group(int(pick.entity_id), provider)
        if hit is None:
            return None
        payload, account = hit if isinstance(hit, tuple) else (hit, "")
        return cascade.CascadeRung("group", "group", pick.entity_id, payload, account)
    if pick.mode == "org":
        hit = probe.org(int(pick.entity_id), provider)
        if hit is None:
            return None
        payload, account = hit if isinstance(hit, tuple) else (hit, "")
        return cascade.CascadeRung("org", "org", pick.entity_id, payload, account)
    if pick.mode == "tenant":
        # ⚠️ **This rung was missing, and its absence was INVISIBLE.** Without it, a
        # `tenant` designation fell through into the platform branch below: the
        # `probe.tenant` probe was never called, the SERVED key became the platform's,
        # and `tenant_budget.enforce` — conditioned on `win.mode ==
        # "tenant"` in the caller — was skipped. The shadow compared two
        # DESIGNATIONS and saw an `accord`: the flag would have silently cancelled
        # piece 1 of L-keys, which is in prod. Dormant as long as no tenant key
        # is set; the first one set would have woken it.
        # The `via` comes from the DESIGNATION (the chain already read the tenant→org edge):
        # re-reading it here would make a second source, and two sources of one
        # verdict end up diverging. It is TRANSLATED into the walker's
        # vocabulary — which says `local` where the chain says `appartenance` — because the
        # served rung must be the one the walker would have produced, byte for byte:
        # `status.py` reads `via == "local"` to mean "personal key configured".
        hit = probe.tenant(pick.entity_id, provider)
        if hit is None:
            return None
        payload, account = hit if isinstance(hit, tuple) else (hit, "")
        return cascade.CascadeRung("tenant", credentials_store.TENANT, pick.entity_id,
                                   payload, account,
                                   via="grant" if pick.via == "grant" else "local")
    # Platform tier: the probe returns the resolved grant (label + secret + quota). The
    # chain already said WHICH instance; `resolve`'s probe reads the one the old
    # path would read. As long as both designate the same one, it is the same key — and when
    # they diverge, the shadow window said so before we switched over.
    grant = probe.platform(sub, provider, org)
    if not grant:
        return None
    return cascade.CascadeRung("platform", credentials_store.PLATFORM,
                               grant.get("label"), grant)
