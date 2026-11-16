"""What is METERED and what is PAID (ADR 0043, ADR 0070 §7).

Two distinct levels, often confused:

- the daily **quota** of a PLATFORM key (`quota_for`, `usage_today`,
  `debiter_plateforme`) — a trial safeguard, lifted by the person's declared
  `platform_unmetered` right in their org (`plafond_du_jour`, `quotas_leves`);
- a connector's **paid option** (`paid_option_for`, `has_option`) — a declared
  right (`entitlements.has_right`), a single rule: the org's, or a right row
  set on the person (in the org or everywhere). The account mark
  (`option_comps`) does not open a paid option.

Depends only on `scope` (the actor's context) and `entitlements` (the declared
rights) — never on `billing`: commerce writes the rights, the core rereads them.
The verdict "is the option OPEN for this connector" (which accounts for BYO)
lives in `views.option_open`, above the cascade.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import entitlements_catalogue as catalogue, providers, db, grants_chain
from ..db import grants as db_grants, usage_org as db_usage_org
from ..mcp_errors import McpError
from . import entitlements, heritage, scope
from .platform_grant import quota_per_org

# DERIVED from the single-source registry (`providers/` package): daily quota per
# provider (fallback if there is no env var nor grant).
_QUOTA_DEFAULTS = providers.QUOTA_DEFAULTS


# Paid add-on required by a connector (layer 3, ADR 0043). None = none. Canonical HOME
# of this mapping (the org AND user surfaces derive from it — derive don't duplicate).
_PAID_OPTION_BY_CONNECTOR = {"unipile": "unipile"}


# The PAID options: a declared right (of the org, or of a person), never an
# account mark. Derived from the mapping above.
_PAID_OPTIONS = frozenset(_PAID_OPTION_BY_CONNECTOR.values())


def paid_option_for(connector: str) -> Optional[str]:
    """Paid option required by a connector (or None).

    Follows **credential delegation**: the six unipile channels have no option
    of their own, they share the account's. One option per channel would be a business
    misreading (the option pays for SEATS on the platform key, and a seat is an account
    at the provider, not a channel) and a regression: a subscribed customer's `unipile`
    comp would stop opening WhatsApp the day of the split."""
    return _PAID_OPTION_BY_CONNECTOR.get(
        connector) or _PAID_OPTION_BY_CONNECTOR.get(
        providers.credential_provider(connector))


def paid_option_refusal(connector: str, sub: "str | None",
                        org: "int | None") -> Optional[str]:
    """The refusal to serve when `connector` is about to consume the PLATFORM key for
    the person `sub` acting in `org`, who does not have (or no longer has) the right to its paid
    option — `None` if nothing opposes it. The right is the org's OR a row
    set on the person, in the org or everywhere (`entitlements.has_right`); `org`
    None: only the person's rows everywhere (and the instance default). **Reread on
    each use** (ADR 0070 §7): a right that has ended stops serving from the next call,
    not at the next account connection.

    The message NAMES the cause and what lifts it; no silent fallback."""
    option = paid_option_for(connector)
    if option is None:
        return None
    if entitlements.has_right(sub, org, option):
        return None
    porteur = providers.REGISTRY.get(providers.credential_provider(connector))
    nom = (porteur.label if porteur and porteur.label else option)
    cle_propre = (f"your own `{providers.credential_provider(connector)}` key is "
                  "still served.")
    if org is None:
        return (f"The \"{nom}\" option is not open to you personally, and no "
                "org that holds it covers this call: work in an org that has "
                f"the option, or get it for yourself; {cle_propre}")
    if sub is None:
        return (f"The \"{nom}\" option is not active for this org: trial ended "
                f"or subscription required; {cle_propre}")
    return (f"The \"{nom}\" option is active neither for this org nor for you: trial "
            f"ended or subscription required; {cle_propre}")


def exiger_option_payante(connector: str, sub: "str | None", org: "int | None") -> None:
    """Raise the refusal of `paid_option_refusal` at the PLATFORM rung of a resolution. The
    rights read are those of the person `sub` and of the org the caller can
    consume: for the beneficiary of a shared project to whom nothing is lent, no
    org (#480, `heritage.org_partagee`) — only their person rows everywhere remain.
    `sub` None (anonymous endpoint): the org alone."""
    org_servie = heritage.org_des_cles(sub, org)
    refus = paid_option_refusal(connector, sub, org_servie)
    if refus:
        if org_servie is not None:
            # WHO lifts the obstacle and WHERE (oto#108) — named to an org member only.
            from .. import detenteurs
            refus += detenteurs.qui_leve_une_option(sub, org_servie)
        raise McpError(ErrorData(code=INVALID_PARAMS, message=refus))


def has_option(sub: str, option: str, *, org: "int | None | object" = scope._UNSET) -> bool:
    """Layer 3 of the connector model (cf. docs/connector-model.md): is the connector
    option `option` unlocked for `sub` in their org? **Single seam.**

    - PAID option (`unipile`): a LIVE declared right (`entitlements.has_right`),
      whatever its source — subscription, gift, partner, trial —, set on the org
      OR on the person (in the org or everywhere, ADR 0070 §7). Without an org, the person's
      rows everywhere. The account mark (`option_comps`, `user_has_option`)
      does NOT open a paid option: only a right row does.
    - Non-paid option (`beta`, a population flag): the account's mark
      (`user_has_option`) or the org's.
    - Any OTHER key of the rights catalog (`platform_unmetered`, `unipile_seats`…)
      **raises** `catalogue_key_via_option_comps`: it is a declared right, which oto-commerce
      sets alone and which `entitlements.value_for` reads (#1097) — reading it in
      `option_comps` would reopen an inherited gift that nobody sets anymore.

    NEVER read the sources directly elsewhere (a new path goes through here).
    Explicit `org` (≠ _UNSET) = computation for a third party against a given org (admin sheet),
    without current_org (context-leak prevention)."""
    if option in _PAID_OPTIONS:
        org = scope.current_org(sub) if org is scope._UNSET else org
        return entitlements.has_right(sub, org, option)
    if catalogue.est_du_catalogue(option):
        raise ValueError(
            f"catalogue_key_via_option_comps: {option!r} is a catalog right — it is "
            "read via `entitlements.value_for`/`has_right`, never in `option_comps`")
    if user_has_option(sub, option):
        return True
    org = scope.current_org(sub) if org is scope._UNSET else org
    return org is not None and db.has_option_comp("org", str(org), option)


def orgs_with_option(sub: str, option: str, orgs: list[int]) -> set[int]:
    """`{o for o in orgs if has_option(sub, option, org=o)}` en deux lectures au plus,
    quel que soit le nombre d'orgs — pour une liste d'orgs (`org.list`). Options NON
    payantes seulement : une option payante ou une clé du catalogue lève, au lieu de
    rendre une réponse que `has_option` ne rendrait pas."""
    if option in _PAID_OPTIONS or catalogue.est_du_catalogue(option):
        raise ValueError(f"orgs_with_option: {option!r} is not a population flag — "
                         "use has_option per org")
    if user_has_option(sub, option):
        return set(orgs)
    return db.orgs_with_option_comp(list(orgs), option)


def user_has_option(sub: str, option: str) -> bool:
    """The ACCOUNT half of the seam — "does THIS ACTOR carry the mark", with no space.

    For NON-paid options only: a paid option is a declared right
    (`entitlements.has_right`), and an account mark does not open it. Some
    questions concern the caller's identity and that alone. `has_option`
    does not fit — it answers true as soon as the ACTIVE ORG carries the mark, so it
    turns an account mark into a space property, shared by all
    members.

    ⚠️ The example that gave birth to it — the `runner_worker` mark on an account —
    no longer exists (09/09/2026): a worker is no longer a marked account, it is a
    machine secret declared in the database (`db.runner_workers`). The distinction
    remains true for any other ACTOR mark: going through `has_option`
    would serve it to **all members** of the org as soon as a gift was set on
    the org or a plan included it.

    The expiry bites in `has_option_comp`, like for all the other surfaces.
    """
    return db.has_option_comp("user", sub, option)


def quota_for(provider: str) -> int:
    """Daily quota of a connector's PLATFORM key.

    Normalized to the credential CARRIER: a quota is a property of the key,
    and six channels that borrow the same key necessarily share its counter. Six
    independent counters would let 6× the quota be consumed on a single key."""
    provider = providers.credential_provider(provider)
    raw = os.environ.get(f"OTO_MCP_QUOTA_{provider.upper()}_DAILY")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _QUOTA_DEFAULTS.get(provider, 0)


def usage_today(sub: str, provider: str) -> int:
    """Today's consumption ON `provider`'s KEY — normalized to its carrier.

    Counterpart of `quota_for`: counter and ceiling must name the same key, otherwise
    a channel would read 0 against the ceiling of an already exhausted key ("quota intact"
    for someone who has nothing left). Every quota reader goes through here."""
    return db.get_usage_today(sub, providers.credential_provider(provider))


def platform_used_today(sub: str, provider: str, edge_counter: Optional[str] = None,
                        usage: Optional[Callable[[], int]] = None, *,
                        org_counter: Optional[int] = None) -> int:
    """The day's count the PLATFORM quota refusal reads (R6, `docs/credits-au-moment-de-l-appel.md`) — SINGLE function, like
    `plafond_du_jour`: the refusal (`resolve._win_quota`) and the `/api/me` snapshot
    (`status.status_for`) read the same number.

    - a connector whose quota counts per ORG (`Connector.grant_quota_per`, the
      platform rung then names `org_counter`) → the org's counter of the day
      (`usage_org`), every member together, whichever scope granted;
    - a `user` edge of the grants chain (`edge_counter`, the grant's key of that name)
      → its edge counter, archived edges included (`db.grants.counter_sum_today`,
      0053-D7);
    - otherwise → `usage(sub, provider, day)`, per person. Under an `org` or `group`
      edge this is deliberate: an edge counter counts for ALL its beneficiaries, and the
      ceiling of an org edge is a per-person ceiling — switching it would change the
      ceiling of every beneficiary. `grant_counters` has no beneficiary column.

    Takes the edge's ref, never the grant: on the resolution path the grant carries the
    DECRYPTED secret, and a frame that raises keeps its locals in the traceback (#564).

    `usage`: the per-person count already read (a preloaded map), `None` reads it."""
    if org_counter is not None:
        return db_usage_org.du_jour(org_counter, providers.credential_provider(provider))
    if edge_counter:
        return db_grants.counter_sum_today(edge_counter, "user", sub)
    return usage() if usage is not None else usage_today(sub, provider)


def compteurs(grant: dict) -> dict:
    """The counter refs of a winning PLATFORM grant, as `platform_used_today` takes them
    (`edge_counter`, `org_counter`) — refs only, never the grant: it may carry the
    decrypted secret, and `platform_used_today` can raise."""
    return {"edge_counter": grant.get("edge_counter"), "org_counter": grant.get("org_counter")}


def quotas_leves(sub: str, org: Optional[int]) -> bool:
    """Does the `platform_unmetered` right lift the platform quotas for the person
    `sub` acting in `org` (ADR 0070 §7)? The right of the org the caller can
    consume — none for the beneficiary of a shared project without a loan (#480) —,
    or a row set on the person, in the org or everywhere."""
    plan_org = heritage.org_des_cles(sub, org)
    return entitlements.has_right(sub, plan_org, entitlements.PLATFORM_UNMETERED)


def plafond_du_jour(grant: dict, provider: str, leves: Callable[[], bool]) -> int:
    """The day's ceiling of a PLATFORM edge `grant` for `provider` — `0` =
    unlimited (registry without a default ceiling, OR quotas lifted by the
    `platform_unmetered` right), never a real ceiling of 0 (`quota_for` does not return it).

    **SINGLE function**: the refusal and the probe (`resolve._win_quota`), the displayed mode
    (`views.credential_mode_for`) and the `/api/me` snapshot (`status.status_for`) all
    go through it. `leves` returns `quotas_leves(sub, org)`; it is only called if there is
    a ceiling to lift — a caller that loops over connectors memoizes it, one
    rights read per snapshot and not per connector."""
    limit = grant.get("daily_quota") or quota_for(provider)
    return 0 if limit and leves() else limit


def debiter_plateforme(sub: str, provider: str, unites: int) -> None:
    """Debits `unites` on `provider`'s PLATFORM key for `sub` — called by the single
    settlement of a call (`reglement.regler_appel`), never by a tool: a tool SAYS what
    it consumes (`units` on the call record), the edge of the call debits it.

    Two counters (blueprint ADR 0053, L5, then R6): the `usage(sub, tool, day)`
    history, per person — the refusal's authority outside a `user` edge — and the EDGE
    counter of 0053-D7, the authority under a `user` edge (`platform_used_today`).
    No-op (and no query) on the edge outside switched connectors.

    BOTH counters debit in ONE go: a call worth N units costs one write, not N
    (a default Maps census would otherwise take 81 pool connections and 81
    transactions for a single tool call, on the hot path of a single-loop server).

    A connector whose quota counts per ORG (`Connector.grant_quota_per`): the counter of
    the org the call is made in takes the units too, in one atomic upsert — the same org
    the platform rung named as `org_counter`, read on the same seam (`scope.current_org`)
    — it is the refusal's authority for that connector; the per-person history is still
    written. Without an org there is nothing to debit: it raises, and the settlement
    journals the lost count."""
    # Metered on the key ACTUALLY consumed (delegation): a WhatsApp call burns
    # the unipile account's quota, not that of a "whatsapp" counter that nobody
    # reads. Write and read (`usage_today`) normalize the same way.
    provider = providers.credential_provider(provider)
    db.increment_usage(sub, provider, unites)
    if quota_per_org(provider):
        org = scope.current_org(sub)
        if org is None:
            raise ValueError(f"usage_org: no org to debit {unites} units of `{provider}` on")
        db_usage_org.ajouter(org, provider, unites)
    if grants_chain.is_chained(provider):
        grants_chain.record_usage(sub, provider, scope.current_org(sub), unites)


def refus_lot(provider: str, label: str, used: int, limit: int, units: int,
              ou_poser: str) -> McpError:
    """The refusal of a BATCH that the day's quota of the shared key does not cover
    (oto#168), NAMED according to its cause. Larger than the WHOLE quota, the batch will never
    pass, even at zero used: say so, and the size that passes, rather than
    "reduce the batch" without a number. Otherwise, it exceeds the rest of the day."""
    entier = units > limit
    debut = (f"this batch ({units}) exceeds the TOTAL quota of key `{label}` ({limit}/day) "
             f"and would be refused even at zero used — split it into batches of ≤ {limit}"
             if entier else
             f"{limit - used} unit(s) left today ({used}/{limit}) on key "
             f"`{label}`, this batch needs {units} — reduce the batch")
    return McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Platform quota {provider}: {debut}, or set your own "
                 f"key{ou_poser} to lift the limit."),
        data={"code": "platform_quota_lot_trop_grand" if entier
              else "platform_quota_lot_depasse_le_reste",
              "units": units, "limit": limit, "used": used}))
