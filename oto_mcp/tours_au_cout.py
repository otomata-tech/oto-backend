"""Model calls billed on their REAL cost: journaled, never priced here.

A hosted agent on a SHARED key (the instance's, or the primary tenant's) of a family
listed in `FAMILLES_AU_COUT` is not reserved nor settled in the units budget
(`access/credits_tours`): the provider answers each call with what it CHARGED for it
(OpenRouter: `usage.cost`, in USD), and oto writes that figure down, one journal line per
turn, for the commerce to bill. oto applies no rate to it: what a dollar costs the
customer is the commerce's grid, not the core's.

Why not the per-token scale of `anthropic`: one OpenRouter model id is served by several
hosts at different prices, and the host that serves a turn is chosen per request
(`provider.order`, then a fallback). A token line is a guess at that host; the cost the
router answered is not.

The line (`tool_calls`, `kind='settlement'`, `journal_jour.KIND_RELEVE`, like the Apify
and Higgsfield settlements): tool = the family (`openrouter`), `quantity` = the cost in
micro-dollars rounded UP (the `jev` rounding), `key_mode` = the rung of the key that paid,
`call_uid` = the turn's key (`credits_tours.cle_du_tour`), `run_id` = the job's run, and
in `args` the job, attempt, turn, the model the provider says it served, the token counts
and the cost in USD. A turn reported twice — even twice AT ONCE (the worker replays a
report it got no answer to) — is journaled once: the line is keyed
(`index_releve.TOUR_AU_COUT`, `INSERT … ON CONFLICT DO NOTHING`).

**The grant's daily quota, in micro-dollars** (`providers/openrouter.py`,
`grant_quota_unit`): on the PLATFORM key, the line's FIRST write debits its cost on that
quota (`quotas.debiter_plateforme`), in the same transaction — a replay debits nothing.
Each call is checked against the quota BEFORE it is sent (`runner_jobs._reserver_un_tour`,
and at launch): used ≥ ceiling refuses (`platform_quota_exhausted`). The cost is only
known after the call, so the overshoot is bounded by ONE turn per job in flight. The
primary tenant's key carries no grant quota: it is journaled, never debited.

What this does NOT do: hold before the call, or refuse a job below a floor of the units
budget. A turn the worker never reports (it died between the call and the report) is
neither journaled nor debited.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from . import access, db, runner_models
from .calllog import truncated_args
from .db import usage
from .db.index_releve import OUTILS_AU_COUT
from .db.journal_jour import KIND_RELEVE

#: The model families whose calls on a shared key are journaled at the cost the provider
#: reported, instead of being reserved and settled in the units budget. Read from the
#: key of their journal lines (`index_releve.OUTILS_AU_COUT`): a family journaled at cost
#: without that key would be written twice on a replay.
FAMILLES_AU_COUT = frozenset(OUTILS_AU_COUT)

#: The rung whose key carries a grant's daily quota (`credentials_store.PLATFORM`).
RANG_A_QUOTA = "platform"


def au_cout(model: Optional[str]) -> bool:
    """Is a call to `model` journaled at its cost (`FAMILLES_AU_COUT`)?"""
    return runner_models.famille(model) in FAMILLES_AU_COUT


def microdollars(cout_usd: float) -> int:
    """A cost in USD as integer micro-dollars, rounded UP. ⚠️ `round` before `ceil`: a
    float such as `3 × 1e-05` reads 3.0000000000000004e-05, which bare `ceil` makes 31."""
    return math.ceil(round(float(cout_usd) * 1e6, 6))


def cout_declare(brut: Any) -> Optional[float]:
    """The cost a worker reported, or None when it is absent or not a finite number ≥ 0."""
    if isinstance(brut, bool) or not isinstance(brut, (int, float)):
        return None
    if not math.isfinite(brut) or brut < 0:
        return None
    return float(brut)


def journaliser_tour(uid: str, *, model: str, sub: Optional[str], org: Optional[int],
                     run_id: Optional[str], servi: Optional[str], cout_usd: float,
                     tokens: dict, key_mode: Optional[str], meta: dict) -> dict:
    """Writes the journal line of one call to `model` (the catalogue id the job names; its
    family is the line's tool), `servi` being what the provider says it served, and on
    the platform key debits its cost on the grant's quota — both in ONE transaction, on
    the line's first write only. Returns `{journaled, microdollars}`, `journaled: False`
    when that turn is already written (nothing is debited then).
    Raises when the journal cannot be written, when `model` is not journaled at cost, or
    when a platform-key turn names no one to debit."""
    tool = runner_models.famille(model)
    if tool not in FAMILLES_AU_COUT:
        raise ValueError(f"{model!r} is not billed at cost ({sorted(FAMILLES_AU_COUT)})")
    if key_mode == RANG_A_QUOTA and not sub:
        raise ValueError("a platform-key turn names no one whose quota it spends")
    montant = microdollars(cout_usd)
    ligne = {
        "kind": KIND_RELEVE, "tool": tool, "sub": sub, "org_id": org, "run_id": run_id,
        "ok": True, "duration_ms": 0, "call_uid": uid, "quantity": montant,
        "key_mode": key_mode,
        # À plat : `truncated_args` rend un objet imbriqué en texte.
        "args": truncated_args({**meta, **tokens, "model": servi or model,
                                "cost_usd": cout_usd}),
    }
    with db.transaction_d_ecriture():
        if not usage.inserer_tour_au_cout(ligne):
            return {"journaled": False, "microdollars": 0}
        if key_mode == RANG_A_QUOTA and montant > 0:
            access.debiter_plateforme(sub, tool, montant)
    return {"journaled": True, "microdollars": montant}
