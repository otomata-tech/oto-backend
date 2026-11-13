"""What the in-app agent may SPEND per account and per day, whatever key pays.

**A ceiling in dollars, per `sub` and per UTC day** (`OTO_COPILOT_DAILY_CAP_USD`, 20 by
default). It does not depend on `OTO_CREDITS_MODE`: the units budget may be in `off` or
`report`, the key may be the org's own — the ceiling holds the same. It bounds what one
account can make the instance spend at its provider in a day, a runaway loop or a script
on a stolen session included; the units budget, when it runs, still charges each call.

**Measured on what the provider answered**, never estimated: each model call adds its
four token counts (`usage`, as the stream reports them — a call cut mid-stream counts what
it consumed) times the provider's list price of the model (`PRIX_PAR_MTOK`). These are
the provider's own prices — what a call COSTS the instance —, not a price the commerce
sells at: the units scale (`anthropic/messages:<model>`) is in units and has no money in
it (`docs/credits-au-moment-de-l-appel.md`), so it cannot give a dollar ceiling.

**Checked before every model call** (`boucle._un_appel`) and before a stream opens
(`routes`): at or past the ceiling, the call is refused, named (`copilot_daily_cap`).
One call may cross it — the next one is refused.

The spend lives in `copilot_spend` (one row per sub and UTC day), purged after 30 days
by the maintenance (`oto-mcp maintenance copilot`).
"""
from __future__ import annotations

import logging
import math
import os
from decimal import Decimal, InvalidOperation

from ...db import copilot as store

logger = logging.getLogger(__name__)

ENV_PLAFOND = "OTO_COPILOT_DAILY_CAP_USD"
#: Refusal code, before a stream (HTTP 429) and inside one (an `error` event).
CODE = "copilot_daily_cap"

#: The provider's list price per million tokens, in dollars, per model and unit (the
#: four units of `access/credits_tours.UNITES_DU_MODELE`). Cache writes are 5-minute
#: ones (1.25 × input): the only TTL the loop asks for.
PRIX_PAR_MTOK: dict[str, dict[str, Decimal]] = {
    "claude-opus-5-5": {
        "input_tokens": Decimal("4"),
        "output_tokens": Decimal("20"),
        "cache_creation_input_tokens": Decimal("5"),
        "cache_read_input_tokens": Decimal("0.20"),
    },
    "claude-sonnet-5-5": {
        "input_tokens": Decimal("2"),
        "output_tokens": Decimal("10"),
        "cache_creation_input_tokens": Decimal("2.50"),
        # 0.05 × input on Sonnet 5.5 (Anthropic pricing page); 0.20 was Sonnet 5's price.
        "cache_read_input_tokens": Decimal("0.10"),
    },
}


class PlafondIllisible(ValueError):
    """`OTO_COPILOT_DAILY_CAP_USD` is set but not a positive amount."""


def plafond_usd() -> Decimal:
    """The ceiling, read on each call. Unset = 20; set but not a positive number (empty
    included) = `PlafondIllisible` — never a silent default: a typo would lift a ceiling
    someone meant to lower."""
    brut = os.environ.get(ENV_PLAFOND, "20").strip()
    try:
        val = Decimal(brut)
    except InvalidOperation:
        raise PlafondIllisible(f"{ENV_PLAFOND}={brut!r} is not an amount in dollars") \
            from None
    if not val.is_finite() or val <= 0:
        raise PlafondIllisible(f"{ENV_PLAFOND}={brut!r} must be a positive amount")
    return val


def cout_micro_usd(modele: str, usage: dict) -> int:
    """What one call cost, in millionths of a dollar, rounded up. A model with no price
    here raises: its calls could not be counted against the ceiling."""
    prix = PRIX_PAR_MTOK.get(modele)
    if prix is None:
        raise KeyError(f"no provider price for `{modele}`: add it to PRIX_PAR_MTOK")
    # tokens × $/Mtok = millionths of a dollar.
    total = sum(Decimal(int((usage or {}).get(u) or 0)) * p for u, p in prix.items())
    return int(math.ceil(total))


def depense_du_jour(sub: str) -> int:
    """Micro-dollars spent today (UTC) by `sub`. Sync (off the loop)."""
    return store.depense_du_jour(sub)


def atteint(sub: str) -> bool:
    """True when `sub` has reached today's ceiling. Sync (off the loop)."""
    return depense_du_jour(sub) >= int(plafond_usd() * 1_000_000)


def message_de_refus() -> str:
    return (f"You have reached today's limit for the in-app agent "
            f"({plafond_usd()} USD of model use). It resets at midnight UTC.")


def noter(sub: str, modele: str, usage: dict) -> int:
    """Adds one call's cost to today's spend of `sub`; returns it. Sync."""
    cout = cout_micro_usd(modele, usage)
    if cout > 0:
        store.ajouter_depense(sub, cout)
    return cout
