"""The DOUBLE READ of L7: the chain computes, the old path decides.

**What this module is not.** It decides nothing, refuses nothing, does not change by a
single byte what is served. It observes. The only visible effect of its existence is one
more row in `access_shadow_l7` — and, the day the window is conclusive, the
right to flip the authority (PR 2), then to remove `walk_cascade` (PR 3).

**What it computes.** The resolution as [0053-D2](blueprint) lays it out:

1. the **reachable set** — the instances of the scopes the subject is a MEMBER of,
   plus those that flow down to them through a live `grants` edge;
2. the **designation** — the call that names an instance and the procedure binding
   take precedence, but they already short-circuit the walk upstream (`resolve`), so what
   remains here is **proximity**: `user > group > org > platform`.

Neither path knows an access restriction on top: 0053-D1 —
restricting means PLACING ownership at the right level, never laying a prohibition
on top. The `connector_acl` table has not been read since 24/09/2026 (the `restriction_acl`
class, which counted its refusals, left with it).

## The discrepancies we can name in advance

Prod survey of 2026-08-29 — they are not anomalies, they are the decisions of
0053 becoming visible. A divergence that fits none of them is `inconnu`,
and it is the only one the window must see at zero.

| class | what produces it |
|---|---|
| `elargissement_equipe` | the cascade only reads the **ACTIVE** team; the reachable set reads **all** the subject's teams in the org. A member of "finance" active in "sales" resolves nothing today and would resolve finance's key tomorrow. **Counted per org**, because it is a served behavior that changes for a named customer |
| `free_tier_hors_modele` | the old path wins the platform rung through the OPEN free tier (`share_mode='open'`, empty `share_down`) — and 0053 has **no** "everyone" beneficiary. It was the only real hole in the model; **decided on 29/08: an explicit "everyone" edge first, the measured phase-out connector by connector afterwards.** This class must therefore fall to **zero** before the removal (PR 3), and it is the edge laid in PR 2 that brings it there |
| `partage_hors_modele` | the platform key is CLOSED on an allowlist (`share_down`) **and no edge expresses it**. Sister of the previous one, different remedy: it is the NAMED edges that are missing. L5's seeding only covered the switched connectors, so any closed key outside that list falls in this case. Experienced on 29/08: 17 observations on `aiark` and `apify` fell into `inconnu` for lack of this name — a perfectly explainable divergence that closed the door for a wrong reason |

## Two method rules, held mechanically

1. **No rule is copied.** The connector's levels are read at their SOURCE —
   the registry (`is_byo_user`, `org_shareable`, `auth_modes`), an instance's
   suspension, the `grants` edges. This module writes a different TRAVERSAL, not
   a second copy of the gates. It is the same discipline as
   the derived visibility (R9) had, which inverted the walker without cloning it.
2. **The comparison is on the RUNG, not on the account.** The choice of a
   multi-identity account is a level of the instance (0053-D9), not an authorization: replaying
   it here would duplicate `_shared_auto_account` to produce a false discrepancy.
   Wiring the resolution to stable instance identifiers is PR 2.

⚠️ **Switch**: `OTO_L7_SHADOW=0` turns everything off (no read, no write).
It is the reversibility lever that does not require a deployment, only a
restart — like the box's other environment levels.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .. import credentials_store, grants_chain, providers
from ..db import access_shadow as db_shadow
from ..db import grants as db_grants
from . import chain_resolution, scope

logger = logging.getLogger(__name__)

# CLOSED vocabulary of classes. A divergence that fits none is `INCONNU` —
# never a sixth value invented at runtime, otherwise the gate to PR 2
# ("zero unknown") would move by itself.
ACCORD = "accord"
ELARGISSEMENT_EQUIPE = "elargissement_equipe"
# Taken from `chain_resolution`, which records them — never redeclared.
FREE_TIER_HORS_MODELE = chain_resolution.FREE_TIER_HORS_MODELE
PARTAGE_HORS_MODELE = chain_resolution.PARTAGE_HORS_MODELE
INCONNU = "inconnu"
CLASSES = (ACCORD, ELARGISSEMENT_EQUIPE, FREE_TIER_HORS_MODELE,
           PARTAGE_HORS_MODELE, INCONNU)

# Flush period of the AGREEMENT, in seconds. Agreement is the nominal case: counting
# it in the database on every call would put a write on the hot path of a single-loop
# server, and make all sessions target the SAME row (the contention measured for R8).
# We accumulate, and flush at most once a minute per (connector, org): the
# denominator stays exact, the price is bounded.
FLUSH_SECONDS = 60


def _enabled() -> bool:
    return (os.environ.get("OTO_L7_SHADOW", "1") or "").lower() not in ("0", "false", "no")


# ── The comparison, and its class ─────────────────────────────────────────────

def _key(x) -> Optional[tuple]:
    """The comparable identity of a verdict: the RUNG and the entity, never the account
    (cf. §2 of the module docstring)."""
    if x is None:
        return None
    return (getattr(x, "mode", None), getattr(x, "entity_type", None),
            str(getattr(x, "entity_id", None)))


def classify(legacy, chain: Optional[chain_resolution.ChainPick], *,
             hors_modele: Optional[str] = None) -> str:
    """The class of a pair of verdicts. PURE function — it is what the test
    exercises on the shapes observed in prod, without a database."""
    if _key(legacy) == _key(chain):
        return ACCORD
    if chain is not None and chain.mode == "group":
        return ELARGISSEMENT_EQUIPE
    if (chain is None and legacy is not None
            and getattr(legacy, "mode", None) == "platform" and hors_modele):
        # The NUANCE comes from the instance's shape, not from one more `if` here.
        return hors_modele
    return INCONNU


def _sample(sub: str, legacy, chain: Optional[chain_resolution.ChainPick]) -> dict:
    """The sample of a divergence, WITHOUT personal data: the sub is hashed
    (enough to cross-reference two occurrences, not to identify anyone), and only
    the rungs and the team involved stay in clear — a team is what we act on,
    a sub is not."""
    def _palier(x) -> str:
        if x is None:
            return "aucun"
        return f"{getattr(x, 'mode', '?')}/{getattr(x, 'entity_type', '?') or '-'}"
    out = {"sub_h": hashlib.md5(sub.encode("utf-8")).hexdigest()[:8],
           "ancien": _palier(legacy), "chaine": _palier(chain)}
    if chain is not None and chain.group_id is not None:
        out["equipe"] = chain.group_id
    if legacy is not None and getattr(legacy, "via", "local") != "local":
        out["ancien_via"] = getattr(legacy, "via")
    return out


# ── The flush: divergence per occurrence, agreement per beat ──────────────────

_lock = threading.Lock()
_accords: dict = {}          # (connector, org_id) -> pending occurrences
_dernier_versement: dict = {}  # (connector, org_id) -> monotonic of the last flush


def _compte_accord(connector: str, org_id: int) -> None:
    """Accumulate an agreement and only flush on the beat. The pending counter is
    reset BEFORE the write: if it fails, we lose a beat, we never
    count twice."""
    cle = (connector, org_id)
    maintenant = time.monotonic()
    with _lock:
        _accords[cle] = _accords.get(cle, 0) + 1
        if maintenant - _dernier_versement.get(cle, 0.0) < FLUSH_SECONDS:
            return
        a_verser = _accords.pop(cle, 0)
        _dernier_versement[cle] = maintenant
    if a_verser:
        db_shadow.bump_shadow(connector, org_id, ACCORD, a_verser)


def observe(provider: str, sub: Optional[str], org: Optional[int], legacy, *,
            want: str = "auto") -> None:
    """Compare the two paths and file the result. **Absolute best-effort**: no
    exception leaves here, no value comes back. Called from `resolve`,
    after the walk."""
    if not sub or not _enabled():
        return
    try:
        porteur = providers.credential_provider(provider)
        chain, hors_modele = chain_resolution.chain_verdict(sub, porteur, org=org, want=want)
        classe = classify(legacy, chain, hors_modele=hors_modele)
        if classe == ACCORD:
            _compte_accord(porteur, int(org or 0))
            return
        db_shadow.bump_shadow(porteur, int(org or 0), classe, 1,
                              _sample(sub, legacy, chain))
        if classe == INCONNU:
            # The only class that must stay at zero: it deserves a log line in
            # addition to the counter, because it calls for a code reading.
            logger.warning(
                "shadow L7: UNKNOWN divergence on %s (org=%s) — old=%s chain=%s "
                "(ADR 0053 L7, double-read window)",
                porteur, org, _key(legacy), _key(chain))
    except Exception:  # noqa: BLE001
        # A shadow that broke a resolution would be worse than no shadow.
        logger.warning("shadow L7: observation failed (%s) — the served resolution "
                       "is NOT affected", provider, exc_info=True)


# ── The INVERSION: who decides, and how we roll back ──────────────────────────
# `OTO_L7_DECIDE=chain` flips the authority — the chain decides, the old path
# computes and compares itself. Rollback is the flag, not a revert: `legacy`
# (the default) restores today's behavior to the byte, and a restart
# is enough. Per process, like the tenant registry: flipping preprod does not flip
# prod.
#
# ⚠️ This flag is only set to `chain` **after** two MEASURED conditions, not
# decided ones: a shadow window with no `inconnu` divergence in PROD (preprod
# traffic does not count), and the `free_tier_hors_modele` class back down to zero — which
# only happens once the `scripts/seed_everyone_edges.py` command has been run.
DECIDE_LEGACY, DECIDE_CHAIN = "legacy", "chain"


def decide_mode() -> str:
    """Who decides in THIS process. Any value other than `chain` means `legacy`: a
    misspelled flag must leave today's behavior, never flip an authority
    by accident."""
    return (DECIDE_CHAIN
            if (os.environ.get("OTO_L7_DECIDE", "") or "").strip().lower() == DECIDE_CHAIN
            else DECIDE_LEGACY)


def chain_decides() -> bool:
    return decide_mode() == DECIDE_CHAIN


def resolution_rungs(sub, provider: str, *, org, group, probe, want="auto"):
    """Served traversal, common to calls and to diagnostics without decryption.

    No observation, consumption or error tolerance here. The anonymous caller keeps
    its existing org-only policy; L7 only changes the identified resolution.
    An explicit context (a third party's sheet) never rereads the requester's.
    """
    from . import cascade
    if sub is not None and chain_decides():
        porteur = providers.credential_provider(provider)
        yield from chain_resolution.rungs_for_picks(
            chain_resolution.chain_paliers(sub, porteur, org=org, want=want, group=group),
            probe, sub, porteur, org)
    else:
        yield from cascade.walk_cascade(sub, provider, org=org, group=group,
                                        probe=probe, want=want)


def decide(provider: str, sub: str, org: Optional[int], *, probe, want: str = "auto",
           group=scope._UNSET):
    """The chain DECIDES, the old path computes and compares itself — the exact mirror of
    PR 1, the authority flipped.

    Returns the served rung, in the same shape as `cascade.cascade_winner`, so that the
    rest of `resolve` (named-account guard, quota, `ResolvedCredential`) does not change
    by a line. Never raises **to observe**; the PROBE's McpErrors (a named
    account not found, a multi-account ambiguity), on the other hand, bubble up as
    before — they are served errors, not observation."""
    porteur = providers.credential_provider(provider)
    # The FETCH keeps the name the walker used to pass it — the traversal changes, the
    # read does not.
    #
    # ⚠️ The read WALKS the rungs, it does not read the one `pick` designates
    # (#673): designation is made on the PRESENCE of a credential, the read at
    # FETCH, and the two diverge on a named account. Stopping at the first one designated
    # produced a flat refusal where the historical path moved on to the next rung.
    # `pick` stays the served DESIGNATION for the window survey — giving it anything else
    # would shift what it measures at the moment we fix the read.
    rung = next(resolution_rungs(sub, provider, org=org, group=group,
                                 probe=probe, want=want), None)
    _observe_inverse(porteur, sub, org, want=want)
    return rung


def _observe_inverse(porteur: str, sub: str, org: Optional[int], *, want: str) -> None:
    """Under the chain's authority, it is the OLD path we survey — and with the
    PRESENCE probe, not fetch: the question asked is "which rung would
    win", and answering it must not decrypt a second key per call.

    The classes are the SAME as on the way out: that is what lets us read a single
    series before and after the switch, instead of two measurements we could not
    compare."""
    if not _enabled():
        return
    try:
        from . import cascade, scope as _scope
        chain, hors_modele = chain_resolution.chain_verdict(sub, porteur, org=org, want=want)
        legacy = cascade.cascade_winner(
            sub, porteur, org=org, group=lambda: _scope.current_group(sub),
            probe=cascade.PRESENCE_PROBE, want=want)
        classe = classify(legacy, chain, hors_modele=hors_modele)
        if classe == ACCORD:
            _compte_accord(porteur, int(org or 0))
            return
        db_shadow.bump_shadow(porteur, int(org or 0), classe, 1,
                              _sample(sub, legacy, chain))
        if classe == INCONNU:
            logger.warning(
                "L7 (chain in command): UNKNOWN divergence on %s (org=%s) — "
                "old=%s chain=%s", porteur, org, _key(legacy), _key(chain))
    except Exception:  # noqa: BLE001
        logger.warning("L7: inverse survey failed (%s) — the resolution SERVED by the "
                       "chain is NOT affected", porteur, exc_info=True)


# ── The seam that `resolve` calls, and that carries the whole lot ────────────
# It lives HERE and not in `resolve` for a reason of subject: the resolution path
# does not need to know that a flag exists, nor how it is written. It asks
# "which rung wins?"; this module answers, and it is the one we read the day we
# remove the old path.

def barreau_gagnant(provider: str, sub: str, org: Optional[int], *, probe,
                    group, want: str = "auto"):
    """The rung that wins — **and a flag says which of the two paths
    designated it.**

    `legacy` (the default): the walker decides, the chain computes alongside and compares itself.
    `chain`: the inverse, identically — same probe, same downstream guards, only the
    TRAVERSAL changes. In both directions the path not taken is surveyed, with the
    same classes, so that a single series of measurements reads before AND after the
    switch. Rollback is the flag and a restart, never a revert.

    Observation never raises and returns nothing: whatever happens, what is
    served is the rung, not the measurement."""
    from . import cascade
    if chain_decides():
        return decide(provider, sub, org, probe=probe, want=want, group=group)
    win = cascade.cascade_winner(sub, provider, org=org, group=group, probe=probe,
                                 want=want)
    observe(provider, sub, org, win, want=want)
    return win
