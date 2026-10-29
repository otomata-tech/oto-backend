"""The units budget at call time: reserve the worst case before the call, settle the
real consumption after (`docs/credits-au-moment-de-l-appel.md`).

The commerce WRITES (scale, deposits, who pays — `capabilities/service_credits.py`), the
core RE-READS and settles. No money here: a budget unit is an integer.

**The perimeter** is declared, never inferred: a tool joins it at registration with
`declarer(tool, connector, function)` — the function whose cost the connectors library
declares (`oto.connectors.declared`), and how to read its arguments from the tool's.
Only a call that goes through a RE-BILLED key is reserved (`rang_refacture`): the
platform's, or the PRIMARY tenant's — both paid by the instance's operator. A call on the
customer's own key, or on a non-primary tenant's (that tenant pays it), is not.

**The path of a call:**

1. the edge opens it (`ouvrir_appel`, on the call record) — tool, arguments;
2. the resolver, on a re-billed key, reserves (`reserver`): the declared worst case
   (`cost.worst_case`) times the scale line, rounded up to whole packages; the payer is
   `org:<id>` (default) or `user:<sub>` (`payer_mode=person`, or no org);
3. the tool says when it SENDS the billed request (`envoi`) and what it got back
   (`reponses`);
4. the edge settles: on success, the real quantity (`cost.settled_quantity`), or the
   collection of an asynchronous job is awaited (`cost.collection_of`); on failure, a
   NET refusal (raised before the request was sent, or a 4xx other than 408 and 429)
   releases, anything else settles at the reserved amount.

A settlement above the worst case is settled as is, with `exceeded: true`: the balance
may go negative there, never at reservation.

**Priced per unit**: an asynchronous function the commerce prices by what the job FOUND,
kind by kind, rather than at the library's single unit, is declared with `unites` — its
worst case per unit, read from the tool's arguments. The hold reserves the sum over the
units' scale lines (its `unit` lists them, joined by `+`), and the collection settles
each unit at what was found (`credits_releve.regler_releve`).

**Matching**: a hold is keyed by its call's `tool_calls.call_uid`, read at reservation;
every settlement says it (`meta.call_id`), with the key's rung (`meta.key_mode`) and,
for an asynchronous job, the job (`meta.job_id`).

**Modes** (`OTO_CREDITS_MODE`): `report` (DEFAULT) runs the whole ledger and refuses
nothing — it logs what `enforce` would refuse; `enforce` refuses with a named code;
`off` reserves nothing. Named refusals: `insufficient_credits`, `credits_unpriced`
(the function has no line in the scale), `spend_ledger_unavailable` (the ledger could
not be written: retryable). An unreadable setting refuses to start (`verifier_reglages`).

Off the event loop: every function here that reads or writes the database is called
from a worker thread (the tool's, or the edge's `run_in_threadpool`).
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import bornes_processus, error_taxonomy, geste, limite_refus_top, session_org, tenancy
from ..db import credits as db_credits
from ..mcp_errors import McpError
from . import credits_modes as modes

logger = logging.getLogger(__name__)

INSUFFISANT = "insufficient_credits"
SANS_BAREME = "credits_unpriced"
INDISPONIBLE = "spend_ledger_unavailable"

_ENV_MODE = "OTO_CREDITS_MODE"
_ENV_HOLD_MAX = "OTO_CREDITS_HOLD_MAX_S"
_ENV_MARGE = "OTO_CREDITS_SYNC_MARGIN_S"
_MODES = ("enforce", "report", "off")
DEFAULT_HOLD_MAX_S = 24 * 3600
DEFAULT_MARGE_S = 60

# The call record key this module owns (`session_org.note_call_trace`). One dict per
# call, mutated in place: the tool's thread and the edge see the same object.
CLE = "credits_appel"

# The scale and the payer modes, re-read at most every `_RELIRE_S` seconds: a call never
# pays a query for them, a change by the commerce bites within that delay.
_RELIRE_S = 30


# ── settings ─────────────────────────────────────────────────────────────────

def mode() -> str:
    """Read on each call. Absent = `report`. Unknown value: raises."""
    # Names LITERAL: the inventory (`env_inventory.py`) finds its reads by the AST.
    lu = os.environ.get("OTO_CREDITS_MODE", "report")
    m = lu.strip().lower()
    if m not in _MODES:
        raise ValueError(f"{_ENV_MODE}={lu!r}: unreadable value, expected one of "
                         f"{', '.join(_MODES)}")
    return m


def _secondes(raw: Optional[str], defaut: int, nom: str) -> int:
    if raw is None or not raw.strip():
        return defaut
    try:
        n = int(raw.strip())
    except ValueError:
        n = -1
    if n < 1:
        raise ValueError(f"{nom}={raw!r}: unreadable value, expected a whole number of "
                         "seconds (1 or more)")
    return n


def hold_max_s() -> int:
    """How long a hold may stay open without a known deadline (an asynchronous job
    without `within_s`, a synchronous function without `timeout_s`)."""
    return _secondes(os.environ.get("OTO_CREDITS_HOLD_MAX_S"), DEFAULT_HOLD_MAX_S,
                     _ENV_HOLD_MAX)


def marge_sync_s() -> int:
    """Added to the deadline of a synchronous call (`_echeance`)."""
    return _secondes(os.environ.get("OTO_CREDITS_SYNC_MARGIN_S"), DEFAULT_MARGE_S,
                     _ENV_MARGE)


def verifier_reglages() -> None:
    """The startup guard: each `OTO_CREDITS_*` setting is read once, or refuses to start."""
    modes.verifier()  # the instance's mode, and the per-function ones (`credits_modes`)
    from .credits_tours import verifier_les_noms  # it reads the model catalogue
    verifier_les_noms(modes.modes_par_fonction())
    hold_max_s()
    marge_sync_s()
    if modes.un_mode_arme() and bornes_processus.reglages().outil_s <= 0:
        logger.warning("crédits : OTO_BORNE_OUTIL_S=0 (borne d'un appel d'outil désarmée) "
                       "— l'échéance d'un appel synchrone sans `timeout_s` déclaré est "
                       "OTO_CREDITS_HOLD_MAX_S (%d s)", hold_max_s())


def _echeance(declare: dict, appel: dict) -> int:
    """The deadline of a hold, in seconds. A synchronous call: max(OTO_BORNE_OUTIL_S,
    the declared `timeout_s`) + the margin — the tool-call bound (#1232) is read
    whatever `OTO_DEBIT_MODE` says: measured or applied, it is how long a call is meant
    to last. An asynchronous submission (its collection gets its own deadline), a call
    outside the MCP bound (a background slice, `jev_rows`), or a disarmed bound without
    `timeout_s` (said at startup): `hold_max_s`."""
    if declare.get("async") is not None or appel.get("hors_borne"):
        return hold_max_s()
    borne = max(bornes_processus.reglages().outil_s, int(declare.get("timeout_s") or 0))
    return borne + marge_sync_s() if borne > 0 else hold_max_s()


# ── the perimeter ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Correspondance:
    """A tool of the perimeter: the connector function whose declared cost it pays, and
    how its arguments read as that function's."""
    outil: str
    connector: str
    function: str
    arguments: Callable[[dict], dict]
    #: Priced per unit: the worst case `{unit: quantity}` from the tool's arguments.
    unites: Optional[Callable[[dict], dict]] = None


_PERIMETRE: dict[str, Correspondance] = {}


def _declare(connector: str, function: str) -> dict:
    from oto.connectors.declared import DECLARED
    return DECLARED.get(connector, {}).get(function, {})


def declarer(outil: str, connector: str, function: str,
             arguments: Callable[[dict], dict] = dict,
             unites: Optional[Callable[[dict], dict]] = None) -> None:
    """Puts `outil` in the perimeter, at its registration. Raises if the connectors
    library declares no cost for `connector.function`: a tool cannot pay a cost
    nobody declared. `unites`: priced per unit, which only an asynchronous function
    can be (its collection reads what was found)."""
    declare = _declare(connector, function)
    if "cost" not in declare:
        raise ValueError(f"{outil}: {connector}.{function} has no declared cost in "
                         "oto.connectors.declared")
    if unites is not None and "async" not in declare:
        raise ValueError(f"{outil}: {connector}.{function} is not asynchronous, it "
                         "cannot be priced per unit found")
    deja = _PERIMETRE.get(outil)
    if deja is not None and (deja.connector, deja.function) != (connector, function):
        raise ValueError(f"{outil} is already declared as {deja.connector}."
                         f"{deja.function}")
    _PERIMETRE[outil] = Correspondance(outil, connector, function, arguments, unites)


def perimetre() -> dict[str, Correspondance]:
    return dict(_PERIMETRE)


# ── the call, as the edge opens it and the tool fills it ─────────────────────

def ouvrir_appel(outil: str, args: Optional[dict], *, hors_borne: bool = False,
                 call_uid: Optional[str] = None) -> None:
    """Called by the edge before the tool runs: a tool of the perimeter gets its
    record (its arguments). No-op for any other tool and outside a call.
    `hors_borne`: an edge the MCP tool-call bound does not cover (a background slice):
    its hold lasts `hold_max_s`. `call_uid`: the `tool_calls.call_uid` of a call that
    is not the gesture's own (a target of `oto_call` or of a recipe, several under one
    gesture); otherwise the reservation reads the gesture's."""
    if outil in _PERIMETRE:
        session_org.note_call_trace(**{CLE: {"outil": outil, "args": dict(args or {}),
                                             **({"hors_borne": True} if hors_borne else {}),
                                             **({"call_uid": call_uid} if call_uid else {})}})


def _appel() -> Optional[dict]:
    releve = session_org.current_call_trace()
    return releve.get(CLE) if releve else None


def envoi() -> None:
    """The tool is about to SEND the billed request: a failure from now on may have
    consumed, and is settled unless it is a net refusal."""
    appel = _appel()
    if appel is not None:
        appel["envoye"] = True


def rien_envoye() -> None:
    """The tool KNOWS its failure consumed nothing although it was past `envoi` (its
    client refused the request before sending it): the failure releases the hold."""
    appel = _appel()
    if appel is not None:
        appel["net"] = True


def reponses(answers: list) -> None:
    """The provider's JSON answers to the billed request(s), for the settlement."""
    appel = _appel()
    if appel is not None:
        appel["reponses"] = list(answers)


# ── the scale and the payer, re-read lazily ──────────────────────────────────

_VERROU = threading.Lock()
_LU: dict = {"a": 0.0, "version": None, "lignes": {}, "personnes": frozenset()}


def _relire() -> dict:
    with _VERROU:
        if time.monotonic() - _LU["a"] < _RELIRE_S:
            return _LU
        version = db_credits.version_du_bareme()
        if version != _LU["version"]:
            _LU["lignes"] = {} if version is None else {
                (l["connector"], l["function"], l["unit"]): (int(l["budget"]), int(l["per_n"]))
                for l in db_credits.lignes_du_bareme(version)}
            _LU["version"] = version
        _LU["personnes"] = frozenset(db_credits.orgs_en_mode_personne())
        _LU["a"] = time.monotonic()
        return _LU


def oublier() -> None:
    """Forgets the scale and the payer modes read (tests, and a process that must see a
    change at once)."""
    with _VERROU:
        _LU.update({"a": 0.0, "version": None, "lignes": {}, "personnes": frozenset()})


def rang_refacture(mode: str, entity_id: Optional[str]) -> Optional[str]:
    """The rung under which a resolved key is RE-BILLED to the org (`platform` or
    `tenant`), or None. The operator pays the platform's key and its primary tenant's;
    a non-primary tenant pays its own, like an org — never reserved. The single
    predicate of both edges: a tool's resolution and a hosted agent's shared key."""
    if mode == "platform":
        return "platform"
    if mode == "tenant" and entity_id == tenancy.primary_slug():
        return "tenant"
    return None


def payeur(sub: str, org: Optional[int], personnes: frozenset) -> str:
    if org is None or org in personnes:
        return f"user:{sub}"
    return f"org:{org}"


def _paquets(quantite, budget: int, per_n: int) -> int:
    return math.ceil(quantite / per_n) * budget


# ── refusals ─────────────────────────────────────────────────────────────────

def _refus(code: str, message: str, retryable: bool, **data) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=message,
                              data={"code": code, "retryable": retryable, **data}))


# ── reserve (the resolver, on a re-billed key) ───────────────────────────────

def reserver(sub: str, org: Optional[int], key_mode: str) -> None:
    """Reserves the worst case of the call in progress, if its tool is in the
    perimeter. Called by the resolver when a re-billed key wins (`check_usage`), once
    or several times per call: the reservation is made once. `key_mode`: its rung
    (`rang_refacture`)."""
    appel = _appel()
    if appel is None or "hold" in appel or "refus" in appel:
        return
    c = _PERIMETRE[appel["outil"]]
    if (m := modes.mode_de(c.connector, c.function)) == "off":
        return
    from oto.connectors import cost
    try:
        if c.unites is not None:
            pire = dict(c.unites(appel["args"]))
        else:
            lib = cost.worst_case(c.connector, c.function, c.arguments(appel["args"]))
            pire = {lib["unit"]: lib["quantity"]}
        if not pire:
            raise cost.CostError(f"{c.outil}: no unit to reserve")
    except cost.CostError:
        # The tool's arguments do not read as the function's: a perimeter declaration
        # to fix, never a refusal the caller could act on.
        appel["refus"] = "cost_unreadable"
        logger.exception("crédits (%s) : pire cas illisible pour %s", m, c.outil)
        if m == "enforce":
            raise
        return
    try:
        lu = _relire()
        lignes = {u: lu["lignes"].get((c.connector, c.function, u)) for u in pire}
        manquent = [u for u, ligne in lignes.items() if ligne is None]
        if manquent:
            appel["refus"] = SANS_BAREME
            limite_refus_top.noter_credits(f"unpriced:{c.connector}.{c.function}",
                                           payeur(sub, org, lu["personnes"]), m, time.time())
            logger.warning("crédits (%s) : %s.%s (%s) sans ligne au barème v%s — %s",
                           m, c.connector, c.function, ", ".join(manquent), lu["version"],
                           "refusé" if m == "enforce" else "servi sans réservation")
            if m == "enforce":
                raise _refus(SANS_BAREME, f"`{c.outil}` has no price in this instance's "
                             "credit scale yet: it cannot be called on the shared key. "
                             "Use your own key, or ask the instance's operator.", False,
                             tool=c.outil)
            return
        reserve = sum(_paquets(q, *lignes[u]) for u, q in pire.items())
        unite, quantite = "+".join(pire), sum(pire.values())
        qui = payeur(sub, org, lu["personnes"])
        delai = _echeance(_declare(c.connector, c.function), appel)
        uid = appel.get("call_uid") or getattr(geste.courant(), "geste_id", None)
        if not uid:
            raise RuntimeError(f"{c.outil}: no gesture, the hold cannot name its call")
        r = db_credits.reserver(
            call_uid=uid, payer=qui, connector=c.connector, function=c.function,
            unit=unite, scale_version=lu["version"], quantity=quantite,
            reserved=reserve, deadline_s=delai, forcer=(m == "report"))
        if r.deja:
            raise RuntimeError(f"{c.outil}: the call {uid} already holds a reservation — "
                               "two calls under one `call_uid`")
    except McpError:
        raise
    except Exception:
        appel["refus"] = INDISPONIBLE
        logger.exception("crédits (%s) : registre indisponible pour %s — %s", m,
                         appel["outil"], "refusé" if m == "enforce" else "servi")
        if m == "enforce":
            raise _refus(INDISPONIBLE, "The credit ledger could not be written; nothing "
                         "was sent nor consumed. Retry in a moment.", True)
        return
    if r.insuffisant:
        logger.warning("crédits (%s) : %s à %d disponibles pour %d réservées (%s) — %s",
                       m, qui, r.disponible, reserve, c.outil,
                       "refusé" if m == "enforce" else "servi")
        if not r.posee:
            appel["refus"] = INSUFFISANT
            # No amount in the text: the budget unit is the instance's own, and a bare
            # number would mislead the reader. The amounts stay in the data.
            raise _refus(INSUFFISANT, f"Not enough credits for `{c.outil}`: the balance "
                         "does not cover the worst case of this call. The call was "
                         "correct: do not retry it as is; top up, or use your own key.",
                         False,
                         tool=c.outil, needed=reserve, available=max(0, r.disponible))
    appel["uid"] = uid
    appel["hold"] = {"payer": qui, "connector": c.connector, "function": c.function,
                     "unit": unite, "quantity": quantite, "reserved": reserve,
                     "lignes": lignes, "key_mode": key_mode}


# ── settle (the edges) ───────────────────────────────────────────────────────

def refus_net(appel: dict, exc: BaseException) -> bool:
    """A failure that consumed nothing: raised before the billed request was sent, or a
    4xx from the provider other than 408 and 429."""
    if appel.get("net") or not appel.get("envoye"):
        return True
    statut = error_taxonomy.upstream_status_in_chain(exc)
    return statut is not None and 400 <= statut < 500 and statut not in (408, 429)


def solder_reussite(releve: Optional[dict]) -> None:
    """The call succeeded: settles the real quantity, or awaits the collection of an
    asynchronous job. Never raises (the edge's settlement guards it)."""
    appel = (releve or {}).get(CLE)
    hold = (appel or {}).get("hold")
    if not hold:
        return
    from oto.connectors import cost
    c, f = hold["connector"], hold["function"]
    answers = appel.get("reponses")
    job = _declare(c, f).get("async")
    if job is not None:
        if not answers:
            raise ValueError(f"{appel['outil']}: an asynchronous submission settles from "
                             "its answer (`credits.reponses`), none was noted")
        releve_job = cost.collection_of(c, f, answers[0])
        delai = releve_job["within_s"] or hold_max_s()
        # What the collection, or the deadline, settles with: the hold keeps it.
        releve_job["meta"] = {**_appariement(appel["uid"], hold),
                              "job_id": releve_job["args"][job["job_id"]["to"]]}
        db_credits.attendre_la_releve(appel["uid"], releve_job, delai)
        return
    if answers is None:
        logger.warning("crédits : %s n'a pas noté ses réponses — réglé au pire cas",
                       appel["outil"])
        limite_refus_top.noter_credits(f"unreported:{appel['outil']}", hold["payer"],
                                       modes.mode_de(c, f), time.time())
        _regler(appel["uid"], hold, hold["quantity"], {"from": "unreported",
                                                       "exceeded": False})
        return
    lu = cost.settled_quantity(c, f, _PERIMETRE[appel["outil"]].arguments(appel["args"]),
                               answers)
    _regler(appel["uid"], hold, lu["quantity"],
            {"from": lu["from"], "exceeded": lu["exceeded"]})


def solder_echec(releve: Optional[dict], exc: BaseException) -> None:
    """The call raised: a net refusal releases, anything else settles at the reserved
    amount. Never raises (the edge's settlement guards it)."""
    appel = (releve or {}).get(CLE)
    hold = (appel or {}).get("hold")
    if not hold:
        return
    if refus_net(appel, exc):
        db_credits.liberer(appel["uid"])
        return
    if db_credits.regler(appel["uid"], montant=hold["reserved"], quantite=hold["quantity"],
                         meta={**_appariement(appel["uid"], hold), "from": "failure",
                               "exceeded": False}) is None:
        _apres_echeance(appel["uid"], hold["reserved"])


def _regler(uid: str, hold: dict, quantite, meta: dict) -> None:
    """A synchronous call, at its single unit (only an asynchronous one is per unit)."""
    montant = _paquets(quantite, *hold["lignes"][hold["unit"]])
    if meta.get("exceeded"):
        logger.warning("crédits : %s.%s a consommé %s %s au-delà du pire cas %s (%s)",
                       hold["connector"], hold["function"], quantite, hold["unit"],
                       hold["quantity"], uid)
    if db_credits.regler(uid, montant=montant, quantite=int(math.ceil(quantite)),
                         meta={**_appariement(uid, hold), **meta}) is None:
        _apres_echeance(uid, montant)


def _appariement(uid: str, hold: dict) -> dict:
    """What matches a settlement to its call: the call, and the key's rung."""
    return {"call_id": uid, "key_mode": hold["key_mode"]}


def _apres_echeance(uid: str, montant: int) -> None:
    """The settlement found its hold closed. Expired (`credits_echeances`): it was
    settled at the amount reserved, and this real settlement is ONLY logged — no
    refund; the commerce may deposit an adjustment. Any other state: already settled
    or released, nothing to say."""
    hold = db_credits.reservation(uid)
    if hold is not None and hold["state"] == db_credits.ECHUE:
        logger.warning("credits_hold_expired_before_settle : %s (%s.%s, payeur %s) réglée "
                       "à l'échéance au montant réservé %s ; le règlement réel (%s) n'est "
                       "que journalisé", uid, hold["connector"], hold["function"],
                       hold["payer"], hold["reserved"], montant)


def echoir(limite: int = 100) -> int:
    """Settles, at the reserved amount and marked `hold_expired`, the holds past their
    deadline (`credits_echeances`). Returns how many were settled here — a hold settled
    meanwhile by its call, or by another process, is skipped. The call or the turn is
    read from the hold's key; the key's rung only an asynchronous hold keeps (its
    `job`): a synchronous hold that expires does not say it."""
    from .credits_tours import appariement
    n = 0
    for uid in db_credits.reservations_echues(limite):
        if db_credits.regler(uid, montant=None, quantite=None,
                             meta={**appariement(uid), "from": "hold_expired",
                                   "exceeded": False,
                                   "hold_expired": True}, etat=db_credits.ECHUE):
            n += 1
    if n:
        logger.info("crédits : %d réservation(s) échue(s) réglée(s) au montant réservé", n)
    return n
