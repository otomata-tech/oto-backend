"""Model calls of a hosted agent on a SHARED key, under the units budget.

Not a tool: the runner worker calls the model itself, on a key the instance re-bills —
the platform's, or the primary tenant's (`credits.rang_refacture`,
`capabilities/runner_jobs`). Each call is a TURN: reserved before it is
sent, at its `max_tokens` of output — the only bound known before the call; the input
and the cache are not reserved (no token count before the call) and settle as
`exceeded` — then settled at the four token counts the provider answered, under the
scale lines of `anthropic.messages:<model>`, one per unit. The MODEL is in the scale
key: models are not priced alike, and a turn is reserved at the price of the model its
job names (`fonction_du_modele`). The scale and the payer are the budget's
(`access/credits`); the mode is that of the model's function (`mode_du_modele`), so
model calls can be enforced while the tools stay in `report`. Before a job starts on a
re-billed key, its payer must have the floor available (`sous_le_plancher`): the first
turn's input is never reserved. A turn's hold is keyed `runner:<job>:<attempt>:<turn>`
(`cle_du_tour`), which every settlement of it says back (`appariement`).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from .. import limite_refus_top
from ..db import credits as db_credits
from . import credits as budget
from . import credits_modes as modes

logger = logging.getLogger(__name__)

MODELE_CONNECTOR = "anthropic"
#: The scale function of a model call: `messages:<model>`.
PREFIXE_DES_MODELES = "messages:"
_DATE = re.compile(r"-\d{8}$")
#: The units of a model call, under the provider's own usage names.
UNITES_DU_MODELE = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens")
_UNITE_RESERVEE = "output_tokens"
_TOUR = re.compile(r"runner:(\d+):(\d+):(\d+)")


def cle_du_tour(job_id: int, tentative: int, tour) -> str:
    """The hold key of one model call: the job, the attempt, the turn (`""`: the prefix
    of every turn of the attempt)."""
    return f"runner:{int(job_id)}:{int(tentative)}:{tour}"


def appariement(uid: str) -> dict:
    """What matches a settlement to what consumed: a turn's job, attempt and turn (the
    names the runner's report uses), or the call (`tool_calls.call_uid`) of any other
    hold."""
    t = _TOUR.fullmatch(uid)
    if t is None:
        return {"call_id": uid}
    return {"runner_job_id": int(t[1]), "attempt": int(t[2]), "turn": int(t[3])}


class TourRefuse(Exception):
    """A turn the ledger refuses (`enforce`): `code` is one of the named refusals, and
    `retryable` says whether the same turn may be asked again."""

    def __init__(self, code: str, message: str, retryable: bool, **data):
        super().__init__(message)
        self.code, self.retryable, self.data = code, retryable, data


def cle_du_modele(model: str) -> str:
    """The model as the scale names it: its id WITHOUT a snapshot date
    (`claude-sonnet-5-20261001` → `claude-sonnet-5`). The catalogue a job names its model
    from (`runner_models`) has no date already; a dated id the provider answers or
    reports reduces to it. A snapshot keeps its model's price; a new model is a new id,
    hence new lines in the scale."""
    return _DATE.sub("", model.strip())


def fonction_du_modele(model: Optional[str]) -> Optional[str]:
    """`messages:<model>`, the scale function of a call to `model`; None without one."""
    if not model or not model.strip():
        return None
    return f"{PREFIXE_DES_MODELES}{cle_du_modele(model)}"


def fonctions_connues(connector: str) -> frozenset[str]:
    """The functions of `connector` the scale can price: a model call of the catalogue
    (`messages:<model>`, `fonction_du_modele`) for the model connector, a
    function with a declared cost for any other. Empty: an unknown connector."""
    if connector == MODELE_CONNECTOR:
        from .. import runner_models
        return frozenset(fonction_du_modele(m.id) for m in runner_models.MODELES
                         if m.family == MODELE_CONNECTOR)
    from oto.connectors.declared import DECLARED
    return frozenset(f for f, d in DECLARED.get(connector, {}).items() if "cost" in d)


def verifier_les_noms(lus: dict[tuple[str, str], str]) -> None:
    """The startup guard of the per-function modes (`credits_modes.modes_par_fonction`),
    here because it reads the model catalogue: every entry names at least one function
    the scale can price, or raises naming it."""
    for connector, function in lus:
        connues = fonctions_connues(connector)
        p = modes._prefixe(function)
        if not connues:
            raise ValueError(f"{modes._ENV_MODE_FONCTIONS}: `{connector}.{function}` — unknown "
                             f"connector `{connector}`: no function of it has a price")
        if (function not in connues if p is None
                else not any(f.startswith(p) for f in connues)):
            raise ValueError(f"{modes._ENV_MODE_FONCTIONS}: `{connector}.{function}` names no "
                             f"priced function of `{connector}` (one of "
                             f"{', '.join(sorted(connues))})")


def mode_du_modele(model: Optional[str]) -> str:
    """The budget mode of a call to `model` (`credits.mode_de` on its scale function); a
    job that names no model reads the mode of `messages:` itself."""
    return modes.mode_de(MODELE_CONNECTOR, fonction_du_modele(model) or PREFIXE_DES_MODELES)


def sous_le_plancher(sub: Optional[str], org: Optional[int],
                     model: Optional[str]) -> Optional[dict]:
    """Before a hosted agent's job starts on a re-billed key: does its payer have the
    floor (`credits.plancher_travail`) available — balance minus open holds? Returns
    None when the job may start, else `{code, needed, available}` — in `enforce` only:
    `report` logs what it would refuse and returns None, and so does `off` or a floor of
    0. `code` names the refusal: `insufficient_credits` (below the floor, final), or
    `spend_ledger_unavailable` when the balance cannot be read (`available: None`) —
    replayable: the job goes back to the queue, neither served nor stopped."""
    plancher = modes.plancher_travail()
    m = mode_du_modele(model)
    if plancher <= 0 or m == "off":
        return None
    try:
        qui = budget.payeur(sub, org, budget._relire()["personnes"])
        s = db_credits.solde(qui)
        disponible = int(s["balance"]) - int(s["holds_open"])
    except Exception:
        logger.exception("crédits (%s) : solde illisible au plancher (org %s) — %s", m,
                         org, "refusé" if m == "enforce" else "servi")
        return ({"code": budget.INDISPONIBLE, "needed": plancher, "available": None}
                if m == "enforce" else None)
    if disponible >= plancher:
        return None
    logger.warning("crédits (%s) : %s à %d disponibles, sous le plancher %d d'un travail "
                   "— %s", m, qui, disponible, plancher,
                   "refusé" if m == "enforce" else "servi")
    return ({"code": budget.INSUFFISANT, "needed": plancher, "available": disponible}
            if m == "enforce" else None)


def _lignes_du_modele(lignes: dict, fonction: Optional[str]) -> Optional[dict]:
    """`{unit: (budget, per_n)}` for the four units, or None if one is missing (or the
    job names no model)."""
    if fonction is None:
        return None
    out = {u: lignes.get((MODELE_CONNECTOR, fonction, u)) for u in UNITES_DU_MODELE}
    return None if any(v is None for v in out.values()) else out


def reserver_tour(uid: str, sub: str, org: Optional[int], max_tokens: int,
                  model: Optional[str]) -> dict:
    """Reserves one model call to `model` before it is sent: `max_tokens` of output, at
    that model's line, rounded up to the scale's packages. Idempotent on `uid` (a
    worker that asks again gets the same answer). Returns `{held, reserved}`; `enforce`
    raises `TourRefuse`, `report` reserves anyway (forced) and logs what it would refuse
    — a model without its four lines is `credits_unpriced` —, `off` holds nothing."""
    m = mode_du_modele(model)
    if m == "off":
        return {"held": False, "reserved": 0}
    try:
        lu = budget._relire()
        fonction = fonction_du_modele(model)
        lignes = _lignes_du_modele(lu["lignes"], fonction)
        if lignes is None:
            nom = fonction or f"{PREFIXE_DES_MODELES}(no model)"
            limite_refus_top.noter_credits(
                f"unpriced:{MODELE_CONNECTOR}.{nom}",
                budget.payeur(sub, org, lu["personnes"]), m, time.time())
            logger.warning("crédits (%s) : %s.%s sans ses quatre lignes au barème v%s "
                           "— %s", m, MODELE_CONNECTOR, nom, lu["version"],
                           "refusé" if m == "enforce" else "servi sans réservation")
            if m == "enforce":
                raise TourRefuse(budget.SANS_BAREME, f"calls to the model `{model}` have "
                                 "no price in this instance's credit scale yet: the "
                                 "shared key cannot be used for it.", False, model=model)
            return {"held": False, "reserved": 0}
        reserve = budget._paquets(max_tokens, *lignes[_UNITE_RESERVEE])
        qui = budget.payeur(sub, org, lu["personnes"])
        r = db_credits.reserver(
            call_uid=uid, payer=qui, connector=MODELE_CONNECTOR, function=fonction,
            unit=_UNITE_RESERVEE, scale_version=lu["version"], quantity=max_tokens,
            reserved=reserve, deadline_s=budget.hold_max_s(), forcer=(m == "report"))
    except TourRefuse:
        raise
    except Exception:
        logger.exception("crédits (%s) : registre indisponible pour le tour %s — %s", m,
                         uid, "refusé" if m == "enforce" else "servi")
        if m == "enforce":
            raise TourRefuse(budget.INDISPONIBLE, "The credit ledger could not be written; "
                             "the model was not called. Retry in a moment.", True) from None
        return {"held": False, "reserved": 0}
    if r.insuffisant:
        logger.warning("crédits (%s) : %s à %d disponibles pour %d réservées (tour %s) — %s",
                       m, qui, r.disponible, reserve, uid,
                       "refusé" if m == "enforce" else "servi")
        if not r.posee:
            # No amount in the text, as for a tool (`credits.reserver`).
            raise TourRefuse(budget.INSUFFISANT, "Not enough credits for this model call: "
                             "the balance does not cover its worst case.",
                             False, needed=reserve, available=max(0, r.disponible))
    return {"held": True, "reserved": reserve}


def regler_tour(uid: str, sub: str, org: Optional[int], tokens: dict,
                meta: dict, model: Optional[str]) -> Optional[dict]:
    """Settles one model call at the four token counts the provider answered, under the
    lines of the model it was reserved for (the hold's function), or of `model` for a
    call that was not reserved (a worker that skipped the reservation): that one is
    reserved now, forced — it has been consumed. Returns `{settled, amount, exceeded}`,
    `settled: False` when the turn was already settled (a report replayed), or None when
    the scale has no price for the model (nothing is written, it is logged), or in `off`
    for a turn it never reserved (nothing is written: `off` only closes what is open).
    Raises when the ledger cannot be written (the caller decides, by mode)."""
    hold = db_credits.reservation(uid)
    if hold is not None and hold["state"] != db_credits.OUVERTE:
        return {"settled": False, "amount": 0, "exceeded": False}
    if hold is None and mode_du_modele(model) == "off":
        return None
    lu = budget._relire()
    # The lines of the version the turn was reserved under, not today's.
    version = lu["version"] if hold is None else int(hold["scale_version"])
    fonction = fonction_du_modele(model) if hold is None else hold["function"]
    hold_lignes = _lignes_du_modele(lu["lignes"] if version == lu["version"] else {
        (l["connector"], l["function"], l["unit"]): (int(l["budget"]), int(l["per_n"]))
        for l in db_credits.lignes_du_bareme(version)}, fonction)
    if hold_lignes is None:
        logger.warning("crédits : tour %s non réglé — %s.%s sans ses quatre lignes au "
                       "barème v%s", uid, MODELE_CONNECTOR, fonction, version)
        return None
    if hold is None:
        sortie = int(tokens[_UNITE_RESERVEE])
        hold = {"reserved": budget._paquets(sortie, *hold_lignes[_UNITE_RESERVEE])}
        db_credits.reserver(
            call_uid=uid, payer=budget.payeur(sub, org, lu["personnes"]),
            connector=MODELE_CONNECTOR, function=fonction, unit=_UNITE_RESERVEE,
            scale_version=version, quantity=sortie, reserved=hold["reserved"],
            deadline_s=budget.hold_max_s(), forcer=True)
    montant = sum(budget._paquets(int(tokens[u]), *hold_lignes[u])
                  for u in UNITES_DU_MODELE)
    depasse = montant > int(hold["reserved"])
    clos = db_credits.regler(uid, montant=montant, quantite=int(tokens[_UNITE_RESERVEE]),
                             meta={**appariement(uid), **meta, "from": "response",
                                   "exceeded": depasse,
                                   "tokens": {u: int(tokens[u]) for u in UNITES_DU_MODELE}})
    if clos is None:
        return {"settled": False, "amount": 0, "exceeded": False}
    return {"settled": True, "amount": montant, "exceeded": depasse}


def clore_les_tours(prefixe: str, key_mode: Optional[str]) -> int:
    """At the end of an attempt, the turns reserved and never reported (the call that
    cut the loop, a worker that died between the call and its report) settle at the
    amount reserved, marked `unreported`, under the rung of the attempt's key. Returns
    how many. Never raises."""
    n = 0
    try:
        for uid in db_credits.reservations_ouvertes(prefixe):
            if db_credits.regler(uid, montant=None, quantite=None,
                                 meta={**appariement(uid), "key_mode": key_mode,
                                       "from": "unreported", "exceeded": False}):
                n += 1
    except Exception:  # noqa: BLE001 — the holds stay open; their deadline settles them
        logger.warning("crédits : tours %s* non clos (leur échéance les règle)", prefixe,
                       exc_info=True)
    if n:
        logger.warning("crédits : %d tour(s) %s* jamais rapporté(s), réglé(s) au montant "
                       "réservé", n, prefixe)
    return n
