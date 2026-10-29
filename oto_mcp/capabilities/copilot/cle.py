"""Which key pays the in-app agent's model calls, and how a call is charged.

Same decision order as a hosted agent's job (`runner_jobs._avec_cle`):

1. The org DEPOSITED its own Anthropic key: used, never metered (the org pays its
   provider). Deposited but unreadable: refused — billing it on a shared key for a vault
   hiccup would make the org pay twice.
2. Otherwise a SHARED key granted to the org (`_cle_exigee.cle_partagee`): when it is a
   RE-BILLED one (the platform's, or the primary tenant's — `credits.rang_refacture`),
   every model call is reserved BEFORE it is sent (its `max_tokens` of output, at the
   model's scale lines) and settled at the four token counts the provider answered —
   the hosted-agent turn path (`access/credits_tours`), keyed `copilot:<thread>:<n>`.
3. Neither: refused, named.

**Never run free on a re-billed key.** A reservation that the ledger could not hold
(`report` mode with no scale lines for the model, or the ledger unwritable) refuses the
call, unlike a hosted agent in `report` mode: the copilot is new, and nothing about it
is grandfathered. `off` is the operator's explicit choice to meter nothing, and is
honoured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ...access import credits as budget
from ...access import credits_tours as tours
from .. import _cle_exigee
from ..runner_jobs import _cle_de_modele, _depot_pose

logger = logging.getLogger(__name__)

DEPOT = "anthropic"


class SansCle(Exception):
    def __init__(self, code: str, message: str, statut: int):
        super().__init__(message)
        self.code, self.message, self.statut = code, message, statut


@dataclass(frozen=True)
class Cle:
    secret: str
    workspace: Optional[str]
    #: `platform` | `tenant` when re-billed (metered), None when the org pays its own.
    payeur: Optional[str]

    def __repr__(self) -> str:   # never the secret, in a log or a traceback
        return f"Cle(payeur={self.payeur!r}, workspace={'set' if self.workspace else None})"


def resoudre(org_id: Optional[int], sub: str) -> Cle:
    """The key for a model call in `org_id` on behalf of `sub`. Raises `SansCle`."""
    if org_id is None:
        raise SansCle("no_org", "Open an organization first: the agent works in the "
                      "organization you are viewing.", 409)
    secret, workspace = _cle_de_modele(int(org_id), DEPOT)
    if secret:
        return Cle(secret, workspace, None)
    depose = _depot_pose(int(org_id), DEPOT)
    if depose is None or depose:
        raise SansCle("model_key_unreadable", "The organization's model key could not be "
                      "read right now. Try again in a moment.", 503)
    if _cle_exigee.cle_exigee(int(org_id), DEPOT):
        raise SansCle("model_key_required", "This organization runs its agents on its own "
                      "model key, and none is set: add an Anthropic key in Connectors.", 402)
    try:
        partagee = _cle_exigee.cle_partagee(int(org_id), DEPOT, sub=sub, strict=True)
    except _cle_exigee.CleIllisible:
        raise SansCle("model_key_unreadable", "The model key could not be read right now. "
                      "Try again in a moment.", 503) from None
    if partagee is None:
        raise SansCle("no_model_key", "This organization has no model key, and the shared "
                      "key is not open to it: add an Anthropic key in Connectors, or ask "
                      "whoever administers this instance for access.", 402)
    # The key's config names the provider workspace its calls are billed to. Unreadable,
    # the call is REFUSED, never sent without it: a call outside its workspace lands on
    # another budget and another rate limit than the one the key was granted under.
    try:
        workspace = (partagee.config or {}).get("workspace_id") or None
    except Exception:
        logger.exception("copilot : config de la clé partagée illisible pour l'org %s — "
                         "appel refusé", org_id)
        raise SansCle("model_key_unreadable", "The model key could not be read right "
                      "now. Try again in a moment.", 503) from None
    return Cle(partagee.key, workspace,
               budget.rang_refacture(partagee.mode, partagee.entity_id))


def uid_du_tour(thread_id: str, rang: int) -> str:
    """The hold key of one model call. Zero-padded: the uid of one call is never the
    prefix of another's (`clore` closes by prefix)."""
    return f"copilot:{thread_id}:{int(rang):06d}"


def reserver(cle: Cle, uid: str, sub: str, org_id: Optional[int], modele: str,
             max_tokens: int) -> None:
    """Reserves one call BEFORE it is sent, on a re-billed key. Raises `SansCle` when the
    ledger refuses or cannot hold it (see the module's rule)."""
    if cle.payeur is None:
        return
    try:
        r = tours.reserver_tour(uid, sub, org_id, max_tokens, modele)
    except tours.TourRefuse as e:
        raise SansCle(e.code, str(e), {budget.INSUFFISANT: 402,
                                       budget.SANS_BAREME: 422}.get(e.code, 503)) from None
    if not r.get("held") and tours.mode_du_modele(modele) != "off":
        raise SansCle(budget.SANS_BAREME, f"Calls to `{modele}` have no price in this "
                      "instance's credit scale yet: the shared key cannot be used for the "
                      "in-app agent.", 422)


def regler(cle: Cle, uid: str, sub: str, org_id: Optional[int], modele: str,
           usage: dict, thread_id: str) -> None:
    """Settles one call at the provider's four token counts. Never raises: the call has
    happened; a failed settlement is logged with what is needed to settle it by hand,
    and the hold's deadline settles it at the reserved amount otherwise."""
    if cle.payeur is None:
        return
    jetons = {u: int(usage.get(u) or 0) for u in tours.UNITES_DU_MODELE}
    try:
        tours.regler_tour(uid, sub, org_id, jetons,
                          {"copilot_thread": thread_id, "model": modele,
                           "key_mode": cle.payeur}, modele)
    except Exception:
        logger.exception("copilot : appel %s NON réglé (jetons %s) — l'échéance de la "
                         "réservation le règlera au montant réservé", uid, jetons)


def clore(cle: Optional[Cle], uid: str) -> None:
    """A call reserved and never settled (stopped mid-stream): settled at the amount
    reserved — it has consumed. Never raises (`clore_les_tours`)."""
    if cle is None or cle.payeur is None:
        return
    tours.clore_les_tours(uid, cle.payeur)


def liberer(cle: Optional[Cle], uid: str) -> None:
    """A call the provider REFUSED before answering (an HTTP error): nothing consumed,
    the hold is released. Never raises."""
    if cle is None or cle.payeur is None:
        return
    from ...db import credits as db_credits
    try:
        db_credits.liberer(uid)
    except Exception:
        logger.warning("copilot : réservation %s non libérée — son échéance la règlera",
                       uid, exc_info=True)
