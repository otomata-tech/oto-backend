"""Apify — rent an already-written scraper instead of writing one (apify.com).

Wraps `oto.tools.apify.client.ApifyClient` (API v2). keyed `api_key` (Bearer),
byo-only: each user/org connects THEIR account — an actor is billed by usage.

Apify is not a scraper but a **catalog of scrapers** (the *actors*): Google
Maps, LinkedIn, Instagram, Amazon, Booking, TikTok… Each actor has its own input
JSON, described on its Store page. Hence the path:

1. `apify_store_search("google maps")` → find the actor and its identifier.
2. `apify_actor(id)` → read its card (default options, memory, timeout).
3. `apify_run_sync(id, input)` → launch and fetch the results (≤ 300 s),
   or `apify_run` + `apify_run_status` + `apify_dataset_items` for a long job.

A running actor costs money: setting `max_items` / `timeout_secs` /
`max_total_charge_usd` AT LAUNCH is the only protection — afterwards, it is billed.
Every launch — `apify_run_sync`, `apify_run`, the dedicated actor tools; a direct call,
`oto_call` or a recipe alike — goes through `_plafond_du_lancement`: its ceiling is
refused above the bound of the call (`_borne_du_lancement`): the org's ceiling ($25 by
default), never above the platform maximum on the platform's key, nor above the ceiling
of the org that lends the key when it is another org's (the stricter of the two: the
lender pays), nor above the platform's safety bound on any org's or person's key — with
no way around it.

Calls to the client are written out in plain (`_client().run(…)`) and not dispatched
by name: that is what makes them verifiable by the version-skew probe.
"""
from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from oto.tools.common.errors import UpstreamHTTPError

from .. import access, session_org
from ..connectors import verify as connector_verify
from .lecture import LECTURE

if TYPE_CHECKING:
    from oto.tools.apify.client import ApifyClient

logger = logging.getLogger(__name__)


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    if status in (401, 403):
        # Apify's own words: a 403 is not always a bad key, and the agent that reads
        # "check the key" alone has nothing to report but its retries.
        return (f"Apify refused the request (HTTP {status}): {str(e.body)[:300]} — do "
                "not retry: report it. If it names the token, check the key configured "
                "on this connector (Apify: Settings → API & Integrations).")
    if status == 402:
        return ("Apify: insufficient credits/plan (402) — top up the account, or "
                "narrow the run's scope (max_items).")
    if status == 404:
        return (f"Apify: not found (404) — check the identifier. An actor is written "
                f"`username/actor-name` (or its id), a run/dataset is an opaque id. {e.body}")
    if status == 408:
        return ("Apify: the run exceeded the 300 s of synchronous mode — relaunch with "
                "`apify_run`, then `apify_run_status` and `apify_dataset_items`.")
    if status == 429:
        return "Apify: too many requests (429) — retry in a moment."
    if status in (500, 502, 503, 504):
        return f"Apify is temporarily unavailable (HTTP {status}) — retry later."
    return f"Apify refused the request (HTTP {status}): {e.body}"


# The states a run never leaves. Its cost is not final there yet: see `_cout`.
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})

# The ceiling of a run launched without one. `max_items` is not a ceiling: a
# pay-per-event actor can charge far beyond it (measured 30/10/2026: `maxItems` 2, about
# 17,000 events). Left unset, Apify's own limit is whatever remains of the account.
#
# `_PLAFOND_DEFAUT_USD` serves when the price or the volume is unknown; the
# `connector_settings` key `CLE_PLAFOND_DEFAUT` (platform scope) replaces it without a
# deploy, read at most once per `_REGLAGE_TTL_S` per process (`_reglage_plafond`).
# `_PLANCHER_USD` keeps a derived ceiling from cutting a small run short, and `_MARGE`
# from cutting one whose items charge more than once.
_PLAFOND_DEFAUT_USD = 5.0
_PLANCHER_USD = 0.05
_MARGE = 2
CLE_PLAFOND_DEFAUT = "default_max_charge_usd"
_REGLAGE_TTL_S = 60.0

# The highest ceiling a launch on the PLATFORM'S key takes, whoever asks: the bound of
# the launch (`_borne_du_lancement`) never exceeds it there. A ceiling asked above the
# bound is refused (`MAX_CHARGE_ABOVE_CAP`), a derived or default one is brought down to
# it. The
# `connector_settings` key `CLE_PLAFOND_MAX` (platform scope) replaces `_PLAFOND_MAX_USD`
# without a deploy, read like `CLE_PLAFOND_DEFAUT`. Unlike the default, an unreadable
# maximum FAILS the launch: it is the spending guard, and a guard that falls back in
# silence guards nothing.
_PLAFOND_MAX_USD = 25.0
CLE_PLAFOND_MAX = "max_charge_cap_usd"
MAX_CHARGE_ABOVE_CAP = "max_charge_above_cap"

# The ceiling per run of an ORG (decision of 03/11/2026): `connector_settings` key
# `CLE_PLAFOND_ORG`, scope `org` (no team scope), set by an org admin
# (`connectors.apify.charge_cap.set_org`); `_PLAFOND_ORG_DEFAUT_USD` when the org has
# not set it. On the platform's key it is bounded by the platform maximum above; on a
# key LENT by another org (`_org_qui_prete`), by that org's own ceiling too — the lender
# pays, the stricter of the two applies; on any org's or person's key (BYOK) by the
# platform's safety bound `CLE_BORNE_BYOK` (platform scope, `_BORNE_BYOK_USD` when not
# set). Read once per launch and per org, out of any loop, remembered for the call
# (`org_settings_for_key`), like the access level. Unreadable, either one FAILS the
# launch, as the maximum does.
CLE_PLAFOND_ORG = "charge_cap_usd"
_PLAFOND_ORG_DEFAUT_USD = 25.0
CLE_BORNE_BYOK = "byok_charge_cap_usd"
_BORNE_BYOK_USD = 1000.0
# Share of the ceiling from which a finished run is said to have reached it.
_SEUIL_ATTEINT = 0.95

# `apify_run_sync` waits this long in all, by slices of at most `_WAIT_SLICE_S` —
# Apify's own cap on `waitForFinish`, and the 300 s the synchronous endpoint used to hold.
_SYNC_BUDGET_S = 300
_WAIT_SLICE_S = 60


def _maintenant() -> float:
    """The clock of `apify_run_sync`'s budget — a seam, so a test can drive it."""
    return time.monotonic()


def _run_of(out: Any) -> Optional[dict]:
    """The run object of a `run` / `run_status` answer — the `{data: …}` envelope of
    the API v2, or a flat body read as the run itself."""
    run = out.get("data") if isinstance(out, dict) else None
    if not isinstance(run, dict):
        run = out if isinstance(out, dict) else None
    return run


def _microdollars(cout: float) -> int:
    """Run cost in micro-dollars, rounded UP — the unit and the rounding of Jev
    (`tools/jev.py::_microdollars`: the `round` before `ceil` keeps float noise from
    billing one micro-dollar too many)."""
    return math.ceil(round(float(cout) * 1e6, 6))


def _gratuit() -> None:
    """Trace the call as FREE (`quantity=0`). A call never leaves its quantity NULL:
    the ledger reads NULL as 1 (`tool_calls.quantity`), and 1 µ$ per read, mixed into
    the micro-dollars of the finished runs, would be billed."""
    session_org.note_call_trace(quantity=0)


def _usd(v: Any) -> Optional[float]:
    """A dollar amount Apify served, or None when it is not a finite, positive number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v) or v < 0:
        return None
    return float(v)


def _cout(run: dict, run_id: str, outil: str) -> Optional[float]:
    """What a FINISHED run costs: `usageTotalUsd` plus the events Apify has counted but
    not yet booked.

    ⚠️ A pay-per-event actor (Google Maps, most of the Store) reaches SUCCEEDED before
    Apify books its events: measured on 14/10/2026, run Dlvmeb19jTs5K0k1a read $0.0002
    at SUCCEEDED (the actor start alone) and $0.0122 nine seconds later, once
    `accountedChargedEventCounts` caught up with `chargedEventCounts`. The biller keeps
    the first figure of a job, so the gap is priced here from the actor's own event
    prices — once booked, the gap is 0 and the figure is the same. None when
    `usageTotalUsd` cannot be read.

    Nothing is guessed: without the booked counts, the gap cannot be told from what
    `usageTotalUsd` already holds — adding every counted event could bill it twice —
    so the figure is `usageTotalUsd` alone, and the log SAYS the gap was not priced.
    An event whose price or counts cannot be read is left out the same way, said."""
    total = _usd(run.get("usageTotalUsd"))
    if total is None:
        return None
    comptes = run.get("chargedEventCounts")
    if not isinstance(comptes, dict) or not comptes:
        return total
    reserves = run.get("accountedChargedEventCounts")
    prix = (((run.get("pricingInfo") or {}).get("pricingPerEvent") or {})
            .get("actorChargeEvents"))
    if not isinstance(reserves, dict) or not isinstance(prix, dict):
        logger.warning("%s %s: counted events without readable booked counts or prices "
                       "(%s, %s), the not-yet-booked gap is not priced",
                       outil, run_id, type(reserves).__name__, type(prix).__name__)
        return total
    for evenement, n in comptes.items():
        unitaire = _usd((prix.get(evenement) or {}).get("eventPriceUsd"))
        deja = reserves.get(evenement, 0)
        if unitaire is None or isinstance(n, bool) or not isinstance(n, int) \
                or isinstance(deja, bool) or not isinstance(deja, int):
            logger.warning("%s %s: event %r has no readable price or counts, its "
                           "not-yet-booked part is not priced", outil, run_id, evenement)
            continue
        total += max(0, n - deja) * unitaire
    return total


def _prix_evenement(evenement: Any) -> Optional[float]:
    """An event's price: its flat price, or its highest tier — the tier of the account
    is not on the actor's card, and a ceiling errs on the high side."""
    if not isinstance(evenement, dict):
        return None
    prix = _usd(evenement.get("eventPriceUsd"))
    if prix is not None:
        return prix
    paliers = evenement.get("eventTieredPricingUsd")
    lus = [_usd(p.get("tieredEventPriceUsd")) for p in paliers.values()
           if isinstance(p, dict)] if isinstance(paliers, dict) else []
    lus = [p for p in lus if p is not None]
    return max(lus) if lus else None


def _plafond_derive(carte: Any, max_items: int) -> Optional[float]:
    """The ceiling a pay-per-event actor's card gives for `max_items` results, or None
    when the actor is not priced per event or its card cannot be read. An item may
    charge several events, so every repeating event counts."""
    data = carte.get("data") if isinstance(carte, dict) else None
    infos = data.get("pricingInfos") if isinstance(data, dict) else None
    tarif = infos[-1] if isinstance(infos, list) and infos else None
    if not isinstance(tarif, dict) or tarif.get("pricingModel") != "PAY_PER_EVENT":
        return None
    evenements = (tarif.get("pricingPerEvent") or {}).get("actorChargeEvents")
    if not isinstance(evenements, dict) or not evenements:
        return None
    par_item = unique = 0.0
    for evenement in evenements.values():
        prix = _prix_evenement(evenement)
        if prix is None:
            return None
        if evenement.get("isOneTimeEvent"):
            unique += prix
        else:
            par_item += prix
    if par_item <= 0:
        return None
    minimum = _usd(tarif.get("minimalMaxTotalChargeUsd")) or 0.0
    return round(max(max_items * par_item * _MARGE + unique, minimum, _PLANCHER_USD), 6)


# `{key: (read at, value)}` of the ceiling settings in this process — see `_reglage`.
# Each entry is replaced whole (atomic under CPython): no lock on reads.
_reglages_lus: dict[str, tuple[float, Optional[str]]] = {}


def _reglage(cle: str) -> Optional[str]:
    """The `apify` setting `cle`, platform scope, or None when it is not set.

    `connector_settings` is not read on the hot path of a call (its module header):
    a launch reads it at most once per `_REGLAGE_TTL_S` in a process, a write reaches
    every deployed color within that delay, without a reload. A failed read is not
    kept: the next launch reads again."""
    from ..db import connector_settings
    lu, maintenant = _reglages_lus.get(cle), time.monotonic()
    if lu is not None and maintenant - lu[0] < _REGLAGE_TTL_S:
        return lu[1]
    valeur = connector_settings.get_connector_setting(
        "platform", connector_settings.PLATFORM_SCOPE_ID, "apify", cle)
    _reglages_lus[cle] = (maintenant, valeur)
    return valeur


def _montant(pose: str) -> Optional[float]:
    """A setting's text as a positive, finite dollar amount, or None."""
    try:
        valeur = float(pose)
    except ValueError:
        return None
    return valeur if 0 < valeur < math.inf else None


def _plafond_par_defaut() -> float:
    """The ceiling when nothing better is known. The setting is a platform decision,
    but a launch never fails on it: unreadable or not a positive amount, the constant
    serves, and the log says so."""
    import psycopg
    try:
        pose = _reglage(CLE_PLAFOND_DEFAUT)
    except psycopg.Error:
        logger.warning("apify: default ceiling setting unreadable, using $%g",
                       _PLAFOND_DEFAUT_USD, exc_info=True)
        return _PLAFOND_DEFAUT_USD
    if not pose:
        return _PLAFOND_DEFAUT_USD
    valeur = _montant(pose)
    if valeur is None:
        logger.warning("apify: setting %s (%r) is not a positive amount, using $%g",
                       CLE_PLAFOND_DEFAUT, pose, _PLAFOND_DEFAUT_USD)
        return _PLAFOND_DEFAUT_USD
    return valeur


def _montant_reglage(cle: str, defaut: float) -> float:
    """The platform setting `cle` as a dollar amount, or `defaut` when it is not set. A
    setting that cannot be read (the base) or is not a positive amount RAISES — no
    launch goes out under a guess."""
    pose = _reglage(cle)
    if pose is None:
        return defaut
    valeur = _montant(pose)
    if valeur is None:
        raise RuntimeError(f"apify: platform setting {cle}={pose!r} is not a "
                           "positive amount of dollars — fix or delete it")
    return valeur


def _plafond_max() -> float:
    """The highest ceiling a launch on the PLATFORM'S key takes: the `CLE_PLAFOND_MAX`
    setting, or `_PLAFOND_MAX_USD`. Unreadable, it raises (`_montant_reglage`)."""
    return _montant_reglage(CLE_PLAFOND_MAX, _PLAFOND_MAX_USD)


def _borne_byok() -> float:
    """The highest ceiling a launch on an org's or a person's OWN key takes: the
    `CLE_BORNE_BYOK` setting, or `_BORNE_BYOK_USD`. Unreadable, it raises."""
    return _montant_reglage(CLE_BORNE_BYOK, _BORNE_BYOK_USD)


def _plafond_de_l_org(org_id: Optional[int]) -> Optional[float]:
    """The ceiling per run the org `org_id` set, or None when it set none. One read per
    call, remembered for it (`org_settings_for_key`). A value that is not a positive
    amount RAISES: interpreting it would be deciding in place of whoever set it."""
    if org_id is None:
        return None
    from ..db import connector_settings
    pose = connector_settings.org_settings_for_key(str(org_id), CLE_PLAFOND_ORG).get("apify")
    if pose is None:
        return None
    valeur = _montant(pose)
    if valeur is None:
        raise RuntimeError(f"apify: setting {CLE_PLAFOND_ORG}={pose!r} of org #{org_id} "
                           "is not a positive amount of dollars — fix or set it again")
    return valeur


def _org_de_l_appel() -> Optional[int]:
    from ..auth.hooks import current_user_sub_from_token
    return access.current_org(current_user_sub_from_token())


def _admin_de(org_id: int) -> bool:
    """Whether the caller is an admin of `org_id` — read on a refusal only."""
    from ..auth.hooks import current_user_sub_from_token
    from .. import roles
    sub = current_user_sub_from_token()
    return bool(sub) and roles.is_org_admin(sub, org_id)


def _sur_cle_plateforme() -> bool:
    """Whether this call resolved the platform's Apify key (`key_mode`, set by the
    key resolution before any launch)."""
    return (session_org.current_call_trace() or {}).get("key_mode") == "platform"


def _org_de_la_ligne(entity_type: str, entity_id: str) -> int:
    """The org a vault row of a keyed connector belongs to: an org's, a team's (its
    org), a member's (the org it is set in, `credentials_store.member_id`). Any other
    row (a person's org-agnostic `user` key, an unknown level) names no org: it RAISES —
    the payer of the run would be a guess."""
    from .. import credentials_store, group_store
    if entity_type == credentials_store.ORG:
        return int(entity_id)
    if entity_type == "group":
        groupe = group_store.get_group(int(entity_id)) or {}
        if groupe.get("org_id") is None:
            raise RuntimeError(f"apify: team #{entity_id}, whose key serves this call, "
                               "belongs to no org — the lender of the key is unknown")
        return int(groupe["org_id"])
    if entity_type == credentials_store.MEMBER:
        return int(entity_id.split(":", 1)[0])
    raise RuntimeError(f"apify: a `{entity_type}` key serves this call — no org owns "
                       "it, the lender of the key is unknown")


def _org_qui_prete(org_id: Optional[int]) -> Optional[int]:
    """The OTHER org whose key serves this call (it lends it: it pays the run), or None
    when the key is the platform's, a tenant's, or belongs to the org of the call itself.

    Read on the row that actually served (`credential_row`, noted by the single resolver
    `access.resolve` with `key_mode`; for a named loan, the lender's row,
    `prets.ligne_servie`). A call whose key is not known RAISES: no launch goes out
    under a guessed payer."""
    from .. import credentials_store
    trace = session_org.current_call_trace() or {}
    mode, ligne = trace.get("key_mode"), trace.get("credential_row")
    if mode in ("platform", credentials_store.TENANT):
        return None
    if not ligne or not ligne[0] or not ligne[1]:
        raise RuntimeError("apify: the key that serves this call is not known (no "
                           "`credential_row` in the call's trace) — the lender of the "
                           "key cannot be told, nothing is launched")
    preteuse = _org_de_la_ligne(ligne[0], ligne[1])
    return None if preteuse == org_id else preteuse


class _Borne(NamedTuple):
    """The highest ceiling of a launch, and who sets it: `org` (the org's ceiling),
    `default` (the org set none), `lender` (the ceiling, or the default, of the org that
    lends the key, `org_id` then names it), `platform` (the platform maximum, platform's
    key) or `byok` (the platform's safety bound, an org's or a person's key)."""
    usd: float
    source: str
    org_id: Optional[int]


def _borne_de_l_org(org_id: Optional[int], source: str) -> _Borne:
    """The ceiling `org_id` set, or the default ($25) when it set none."""
    de_l_org = _plafond_de_l_org(org_id)
    return (_Borne(de_l_org, source, org_id) if de_l_org is not None
            else _Borne(_PLAFOND_ORG_DEFAUT_USD, "default" if source == "org" else source,
                        org_id))


def _borne_du_lancement() -> _Borne:
    """The org's ceiling (or the default), bounded by the platform maximum on the
    platform's key; on a key lent by another org, by that org's ceiling too (the stricter
    of the two: the lender pays); on any org's or person's key, by the safety bound."""
    org_id = _org_de_l_appel()
    borne = _borne_de_l_org(org_id, "org")
    if _sur_cle_plateforme():
        maximum = _plafond_max()
        return _Borne(maximum, "platform", org_id) if maximum < borne.usd else borne
    preteuse = _org_qui_prete(org_id)
    if preteuse is not None:
        d_elle = _borne_de_l_org(preteuse, "lender")
        borne = d_elle if d_elle.usd < borne.usd else borne
    byok = _borne_byok()
    return _Borne(byok, "byok", org_id) if byok < borne.usd else borne


def _nom_de_la_borne(borne: _Borne) -> str:
    """Which bound applies, in words."""
    if borne.source == "platform":
        return (f"this platform's maximum of ${borne.usd:g} per run on the platform's "
                "Apify key")
    if borne.source == "byok":
        return (f"this platform's safety bound of ${borne.usd:g} per run, even on your "
                "own Apify key")
    if borne.source == "lender":
        return (f"the ceiling of organization #{borne.org_id}, which lends this Apify "
                f"key and pays its runs, of ${borne.usd:g} per run")
    quoi = ("your organization's ceiling" if borne.source == "org"
            else "the default ceiling")
    return f"{quoi} of ${borne.usd:g} per run"


def _qui_la_regle(borne: _Borne) -> str:
    """The bound, who sets it and — for an admin of the org — how to change it. Read
    only on a refusal: the admin check costs nothing to a launch that goes out."""
    texte = _nom_de_la_borne(borne)
    if borne.source == "platform":
        return (f"{texte} — an organization goes beyond it only on its own Apify key, "
                "and its admins set its ceiling")
    if borne.source == "byok" or borne.org_id is None:
        return texte
    if _admin_de(borne.org_id):
        return (f"{texte} — as an admin of organization #{borne.org_id}, you change it "
                f"with `oto_org_settings(op=\"set\", domain=\"apify_charge_cap\", "
                f"org_id={borne.org_id}, usd=…)` (or PUT /api/orgs/{borne.org_id}/"
                "connectors/apify/charge-cap, `{\"usd\": …}`)")
    return f"{texte}, set by the admins of organization #{borne.org_id}"


def _plafond(usd: float, borne: Optional[_Borne] = None) -> float:
    """A ceiling asked by the caller, refused out of (0, bound of the launch] — never
    clamped in silence. The one check of every launch, whoever calls."""
    borne = borne or _borne_du_lancement()
    if not 0 < usd <= borne.usd:
        if usd > borne.usd:
            raise _bad(f"{MAX_CHARGE_ABOVE_CAP}: `max_total_charge_usd` ${usd:g} is above "
                       f"{_qui_la_regle(borne)}. Nothing was launched; split the work "
                       "into smaller runs.")
        raise _bad(f"`max_total_charge_usd` must be above 0 (got {usd:g}).")
    return usd


def _plafond_du_lancement(client: "ApifyClient", actor_id: str, max_items: Optional[int],
                          demande: Optional[float]) -> float:
    """The `max_total_charge_usd` a run starts with: the caller's (refused above the
    bound of the launch, `_plafond`), or the derived one, or the default — those two
    brought down to the bound, and the response SAYS so (`_dire_plafond_ramene`): the
    run may stop early with a partial output. Never None: an unset ceiling is the whole
    account's balance.

    Reading the actor's card never fails a launch: an Apify refusal (`McpError`, from
    `_upstream`) or a network failure (`requests.RequestException`: connection,
    timeout) falls back to the default ceiling, and the log says why."""
    import requests
    borne = _borne_du_lancement()
    if demande is not None:
        return _plafond(demande, borne)
    if max_items and max_items > 0:
        try:
            with _upstream():
                derive = _plafond_derive(client.actor(actor_id), max_items)
        except (McpError, requests.RequestException) as e:
            logger.warning("apify: card of %s unreadable (%s: %s), default ceiling",
                           actor_id, type(e).__name__, e)
            derive = None
        if derive is not None:
            return _ramener(derive, borne)
    return _ramener(_plafond_par_defaut(), borne)


def _ramener(usd: float, borne: _Borne) -> float:
    """`usd` brought down to the bound of the launch — never in silence."""
    if usd <= borne.usd:
        return usd
    _dire_plafond_ramene(usd, borne)
    return borne.usd


def _dire_plafond_ramene(usd: float, borne: _Borne) -> None:
    """A ceiling derived from the run's size (or the default) above the bound of the
    launch: the run starts at the bound, and may stop before its end."""
    session_org.noter_avis(
        f"Apify: this run needs a ceiling of about ${usd:.2f}, above "
        f"{_nom_de_la_borne(borne)}: it starts at ${borne.usd:g} and may stop "
        "early, with a partial output. To get everything, split the work into smaller "
        "runs (a lower `max_items` each).")


def _noter_lancement(run_id: str, actor_id: str, outil: str,
                     plafond: Optional[float]) -> None:
    """Record a run started on the platform key, so that a run nobody reads back is
    still billed (`apify_releve.py`). A customer's own key pays Apify directly. Never
    blocks the run: a miss is logged, and the monthly comparison shows it."""
    releve = session_org.current_call_trace() or {}
    if releve.get("key_mode") != "platform":
        return
    from ..auth.hooks import current_user_sub_from_token
    from ..db import apify_runs
    try:
        sub = current_user_sub_from_token()
        apify_runs.noter(run_id, org_id=access.current_org(sub), sub=sub, outil=outil,
                         actor_id=actor_id, run_id=session_org.current_call_run(),
                         plafond_usd=plafond)
    except Exception:
        logger.warning("%s %s: launch not recorded", outil, run_id, exc_info=True)


def _hors_du_lanceur(run_id: str, statut: str, cout: float) -> bool:
    """Whether this read must trace 0 WITHOUT the job, the launch record being the
    one that bills (`apify_releve.py`). Only a platform-key read looks the table up:
    a customer's own key pays Apify directly, and its runs are never recorded.

    - read by the org that launched it: billed here, and the record is settled, so
      the nightly settlement does not bill it again;
    - read from another org (a platform operator looking at it): billed to nobody
      here, the settlement bills the launcher;
    - not recorded (launched before the table): billed where it is read, as always;
    - the record cannot be read: billed to nobody here, and said. Billing the reader
      would bill the run twice when it was recorded — the reader now, the launcher at
      the settlement. A run older than the table whose record lookup fails is lost
      instead: the log names it, and the monthly comparison sees the gap."""
    if (session_org.current_call_trace() or {}).get("key_mode") != "platform":
        return False
    import psycopg

    from ..db import apify_runs
    try:
        ligne = apify_runs.lire(run_id)
    except psycopg.Error:
        logger.warning("apify %s: launch record unreadable, cost traced 0 without the "
                       "job (the settlement bills the launcher)", run_id, exc_info=True)
        return True
    if ligne is None:
        return False
    from ..auth.hooks import current_user_sub_from_token
    if ligne["org_id"] != access.current_org(current_user_sub_from_token()):
        return True
    try:
        apify_runs.marquer(run_id, statut=statut, cout_usd=cout)
    except psycopg.Error:
        logger.warning("apify %s: cost not recorded on the launch", run_id, exc_info=True)
    return False


def _dire_plafond_atteint(run: dict, run_id: str, cout: float) -> None:
    """A run stopped by its ceiling ends SUCCEEDED, with a partial output: say it."""
    options = run.get("options")
    plafond = _usd(options.get("maxTotalChargeUsd")) if isinstance(options, dict) else None
    if plafond and cout >= plafond * _SEUIL_ATTEINT:
        session_org.noter_avis(
            f"Apify run {run_id} cost ${cout:.4f} of its ${plafond:g} ceiling: it may "
            "have stopped early and its output may be partial. To get more, launch it "
            "again with a higher `max_total_charge_usd` (at most your organization's "
            "ceiling per run), or split the work into smaller runs.")


def _releve(out: Any, run_id: str, outil: str = "apify_run_status") -> None:
    """Record a FINISHED run's real cost on the current call's trace: `quantity` in
    micro-dollars, and the run as the JOB of the line (`apify_run_id`, served as
    `job_id` by the billing lens) — a finished run polled N times traces N times the
    same cost, and the biller counts a job once.

    ⚠️ While the run is not in a terminal state, `usageTotalUsd` is "what it cost SO
    FAR": the poll traces 0 WITHOUT the job — the poll itself is free, and a biller
    that keeps the first figure it sees for a job would keep a partial one. A response
    whose state or cost cannot be read traces 0 without the job too, and SAYS so in
    the log (run id and type only): an absent figure is not a zero, a guessed one would
    be billed, and a silent one would be lost. A terminal run at 0 traces 0 with its
    job — measured, nothing to bill.

    ⚠️ A platform-key run belongs to the org that LAUNCHED it: read from another org
    (a platform operator looking at it), or when its launch record cannot be read, it
    traces 0 without the job, and the settlement bills the launcher
    (`_hors_du_lanceur`, `apify_releve.py`)."""
    run = _run_of(out)
    if run is None:
        logger.warning("%s %s: unreadable response (%s), cost not recorded",
                       outil, run_id, type(out).__name__)
        _gratuit()
        return
    status = run.get("status")
    if not isinstance(status, str):
        logger.warning("%s %s: unreadable status (%s), cost not recorded",
                       outil, run_id, type(status).__name__)
        _gratuit()
        return
    if status not in _TERMINAL:
        _gratuit()
        return
    cout = _cout(run, run_id, outil)
    if cout is None:
        logger.warning("%s %s: finished (%s) without a readable cost (%s), cost not "
                       "recorded", outil, run_id, status,
                       type(run.get("usageTotalUsd")).__name__)
        _gratuit()
        return
    rid = run.get("id")
    _dire_plafond_atteint(run, run_id, cout)
    if _hors_du_lanceur(run_id, status, cout):
        _gratuit()
        return
    session_org.note_call_trace(
        quantity=_microdollars(cout),
        apify_run_id=rid if isinstance(rid, str) and rid else run_id)


_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"


def _verify(fields: dict, config: dict | None = None) -> dict:  # noqa: ARG001
    """"Test the connection" probe — covers `auth+quota`.

    `GET /v2/users/me/limits`: authenticated, free, and the only Apify call that says
    how much of the month is left — `limits.maxMonthlyUsageUsd` against
    `current.monthlyUsageUsd`. Listing actors (the former probe) authenticated just
    as well on an account that had hit its cap, and every run then failed upstream.

    The balance is in US DOLLARS of platform usage, not in credits: Apify bills
    compute and proxy, not results.
    """
    import requests

    r = requests.get(_LIMITS_URL, headers={"Authorization": f"Bearer {fields['key']}"},
                     timeout=15)
    if r.status_code in (401, 403):
        raise connector_verify.NonAutorise(
            f"Apify refused the token (HTTP {r.status_code}).")
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    plafond = connector_verify.nombre_lu((data.get("limits") or {}).get("maxMonthlyUsageUsd"))
    consomme = connector_verify.nombre_lu((data.get("current") or {}).get("monthlyUsageUsd"))
    if plafond is None or consomme is None:
        raise RuntimeError(
            f"Apify answered without a readable monthly usage: {str(data)[:200]}")
    restant = round(plafond - consomme, 2)
    if restant <= 0:
        raise connector_verify.QuotaEpuise(
            f"The Apify token is good, but the monthly usage cap is reached "
            f"(${consomme:.2f} of ${plafond:.2f}). Raise the cap or wait for the next "
            "cycle — reconnecting would change nothing.")
    return {"quota": {"restant": restant, "unite": "usd", "limite": plafond}}


def _client() -> ApifyClient:
    from oto.tools.apify.client import ApifyClient

    key, _ = access.resolve_api_key("apify")
    return ApifyClient(api_key=key)


@contextmanager
def _upstream():
    """Turn an Apify refusal into an actionable tool error."""
    try:
        yield
    except ValueError as e:
        raise _bad(str(e))
    except UpstreamHTTPError as e:
        raise _bad(_upstream_message(e))


@contextmanager
def _upstream_du_run(run_id: str):
    """`_upstream` for a run that already exists: the refusal names the run, and
    says how to pick it up — never a bare error that leads to a second launch."""
    reprise = (f" Apify run {run_id} was started: pick it up with "
               f"`apify_run_status(run_id=\"{run_id}\")`, then `apify_dataset_items`.")
    try:
        yield
    except ValueError as e:
        raise _bad(str(e) + reprise)
    except UpstreamHTTPError as e:
        raise _bad(_upstream_message(e) + reprise)


def lancer_acteur(
    actor_id: str,
    run_input: Optional[dict],
    *,
    outil: str,
    max_items: Optional[int] = None,
    limit: Optional[int] = None,
    fields: Optional[list[str]] = None,
    timeout_secs: Optional[int] = None,
    memory_mbytes: Optional[int] = None,
    max_total_charge_usd: Optional[float] = None,
) -> Any:
    """Run `actor_id`, wait for it up to 300 s, bill it once finished, return its items.

    The body of `apify_run_sync`, shared with the dedicated actor tools
    (`tools/apify_actors.py`): `outil` is the tool that answers, named in the cost
    warnings. Returns the LIST of items — or `{error, run_id, status, items}` for a
    run that ended other than SUCCEEDED, or whose output could not be read.
    """
    # Not the synchronous endpoint: it answers with the items only — no run id,
    # no cost — so a run through it could never be billed. Start the run, wait
    # for it by slices, then read its dataset: the finished run carries its cost.
    debut = _maintenant()
    client = _client()
    plafond = _plafond_du_lancement(client, actor_id, max_items, max_total_charge_usd)
    with _upstream():
        out = client.run(
            actor_id, run_input=run_input, max_items=max_items,
            timeout_secs=timeout_secs, memory_mbytes=memory_mbytes,
            max_total_charge_usd=plafond,
            wait_for_finish=_WAIT_SLICE_S)
    run = _run_of(out)
    run_id = run.get("id") if run else None
    if not isinstance(run_id, str) or not run_id:
        raise _bad(f"Apify started {actor_id} without a readable run id — "
                   "find the run with `apify_actors` / the Apify console.")
    _noter_lancement(run_id, actor_id, outil, plafond)
    # From here the run EXISTS: every refusal names it, so the agent can pick it
    # up with `apify_run_status` instead of launching (and paying for) it again.
    with _upstream_du_run(run_id):
        while run.get("status") not in _TERMINAL:
            reste = _SYNC_BUDGET_S - (_maintenant() - debut)
            if reste < 1:
                # Never aborted behind the agent's back, never billed here: the
                # run is still going, `apify_run_status` records its cost once
                # it has finished.
                _gratuit()
                raise _bad(
                    f"Apify run {run_id} ({actor_id}) is still {run.get('status')} "
                    f"after {_SYNC_BUDGET_S} s. It keeps running: poll it with "
                    f"`apify_run_status(run_id=\"{run_id}\")`, then read its output "
                    "with `apify_dataset_items`.")
            out = client.run_status(
                run_id, wait_for_finish=min(_WAIT_SLICE_S, int(reste)))
            run = _run_of(out) or {}
    # Finished: billed NOW, before reading the output — a dataset that cannot be
    # read must not leave a finished run unbilled. Only an ANSWERED call is billed
    # (`tool_calls.ok`), so from here nothing is raised: a failed run or an
    # unreadable dataset is answered, with the run, never thrown.
    _releve(out, run_id, outil=outil)
    status = run.get("status")
    dataset_id = run.get("defaultDatasetId")
    try:
        items = client.dataset_items(dataset_id, limit=limit, fields=fields)
    except (ValueError, UpstreamHTTPError) as e:
        detail = (_upstream_message(e) if isinstance(e, UpstreamHTTPError)
                  else str(e))
        return {"error": f"Apify run {run_id} ({actor_id}) ended {status}, but its "
                         f"output could not be read: {detail} Read it with "
                         f"`apify_dataset_items(dataset_id=\"{dataset_id}\")` — the "
                         "run is billed, do not launch it again.",
                "run_id": run_id, "status": status, "dataset_id": dataset_id,
                "items": None}
    if status != "SUCCEEDED":
        return {"error": f"Apify run {run_id} ({actor_id}) ended {status}. Its log: "
                         f"`apify_run_status(run_id=\"{run_id}\")`.",
                "run_id": run_id, "status": status, "items": items}
    return items


def register(mcp: FastMCP) -> None:
    connector_verify.register("apify", _verify, couvre=connector_verify.AUTH_QUOTA)

    # --- find the actor -----------------------------------------------------

    @mcp.tool(annotations=LECTURE)
    def apify_store_search(
        search: Optional[str] = None,
        limit: int = 20,
        category: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """Search Apify's public Store for an actor that already scrapes your target.

        Start here: naming a target ("google maps reviews", "linkedin company",
        "amazon products") is how you find the actor to run, along with its pricing
        and its identifier.

        Returns — `{data: {items: [{id, username, name, title, description, stats,
            pricingInfos, …}], total}}`. Run it with `username/name`.

        Args:
            search: free text describing the site or data you want.
            category: Store category (e.g. `"SOCIAL_MEDIA"`, `"ECOMMERCE"`).
            sort_by: `"relevance"` | `"popularity"` | `"newest"` | `"lastUpdate"`.
        """
        with _upstream():
            out = _client().store_search(search=search, limit=limit,
                                         category=category, sort_by=sort_by)
        _gratuit()
        return out

    @mcp.tool(annotations=LECTURE)
    def apify_actors(limit: int = 50, offset: Optional[int] = None) -> dict:
        """List the actors of THIS account (not the public Store)."""
        with _upstream():
            out = _client().actors(limit=limit, offset=offset)
        _gratuit()
        return out

    @mcp.tool(annotations=LECTURE)
    def apify_actor(actor_id: str) -> dict:
        """Fetch one actor's card: builds, versions and `defaultRunOptions`
        (default memory and timeout).

        Read it before a first run to size `memory_mbytes`/`timeout_secs`. The
        actor's INPUT fields are documented on its Store page, not here.

        Args:
            actor_id: `username/actor-name` (as shown in the Store) or its id.
        """
        with _upstream():
            out = _client().actor(actor_id)
        _gratuit()
        return out

    # --- launch -------------------------------------------------------------

    @mcp.tool()
    def apify_run_sync(
        actor_id: str,
        run_input: Optional[dict] = None,
        max_items: Optional[int] = None,
        limit: Optional[int] = None,
        fields: Optional[list[str]] = None,
        timeout_secs: Optional[int] = None,
        memory_mbytes: Optional[int] = None,
        max_total_charge_usd: Optional[float] = None,
    ) -> Any:
        """Run an actor and return its results in one call. Waits up to 300 s.

        The normal path for a bounded scrape. A run still going after 300 s is NOT
        stopped: the error names its run id — continue with `apify_run_status` then
        `apify_dataset_items`. For a scrape you know is long, use `apify_run`.

        Returns: the LIST of dataset items — or, for a run that ended FAILED, TIMED-OUT
        or ABORTED, `{error, run_id, status, items}` (that run is billed too).

        Args:
            actor_id: `username/actor-name` or id.
            run_input: the actor's own input JSON — its fields are specific to each
                actor (e.g. `{"searchStringsArray": ["bakery Marseille"],
                "maxCrawledPlaces": 20}` for the Google Maps scraper). See the
                actor's Store page.
            max_items: results wanted — set it. It caps billed items on
                pay-per-result actors ONLY: a pay-per-event actor can charge far beyond
                it, `max_total_charge_usd` is what stops it.
            limit / fields: paginate and project the returned items (some actors
                return very wide objects).
            timeout_secs / memory_mbytes: run budget on Apify's side.
            max_total_charge_usd: hard cost ceiling for this run, at most your
                organization's ceiling per run ($25 unless its admins set another;
                on the platform's key, never above the platform maximum; on a key
                another organization lends, never above that organization's ceiling;
                above it the run is refused, `max_charge_above_cap`). Omitted, it is derived from
                `max_items` and the actor's event prices, or $5 by default. A run
                that reaches it stops, with a partial output.
        """
        return lancer_acteur(
            actor_id, run_input, outil="apify_run_sync", max_items=max_items,
            limit=limit, fields=fields, timeout_secs=timeout_secs,
            memory_mbytes=memory_mbytes, max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_run(
        actor_id: str,
        run_input: Optional[dict] = None,
        max_items: Optional[int] = None,
        timeout_secs: Optional[int] = None,
        memory_mbytes: Optional[int] = None,
        max_total_charge_usd: Optional[float] = None,
        wait_for_finish: Optional[int] = None,
    ) -> dict:
        """Start an actor WITHOUT waiting for it — for scrapes longer than 300 s.

        Returns — `{data: {id, status, defaultDatasetId, …}}` — keep `id` for
            `apify_run_status` and `defaultDatasetId` for `apify_dataset_items`.

        Args:
            max_items / max_total_charge_usd: as in `apify_run_sync`.
            wait_for_finish: seconds to wait before returning (max 60) — enough to
                catch a short run without polling.
        """
        client = _client()
        plafond = _plafond_du_lancement(client, actor_id, max_items, max_total_charge_usd)
        with _upstream():
            out = client.run(
                actor_id, run_input=run_input, max_items=max_items,
                timeout_secs=timeout_secs, memory_mbytes=memory_mbytes,
                max_total_charge_usd=plafond,
                wait_for_finish=wait_for_finish)
        run = _run_of(out)
        if run and isinstance(run.get("id"), str) and run["id"]:
            _noter_lancement(run["id"], actor_id, "apify_run", plafond)
        # Launching is free; the cost comes from `apify_run_status`, or the settlement.
        _gratuit()
        return out

    @mcp.tool(annotations=LECTURE)
    def apify_run_status(run_id: str, wait_for_finish: Optional[int] = None) -> dict:
        """Check a run: `status` (READY, RUNNING, SUCCEEDED, FAILED, TIMED-OUT,
        ABORTED), `defaultDatasetId` (where the output is) and `usageTotalUsd`
        (what it cost so far)."""
        with _upstream():
            out = _client().run_status(run_id, wait_for_finish=wait_for_finish)
        _releve(out, run_id)
        return out

    @mcp.tool()
    def apify_abort_run(run_id: str, gracefully: Optional[bool] = None) -> dict:
        """Abort a running actor — stops the billing.

        Args:
            gracefully: let the actor finish its current item and flush its results
                (a hard abort may lose what wasn't pushed yet).
        """
        with _upstream():
            out = _client().abort_run(run_id, gracefully=gracefully)
        # Aborting is free; the run's final cost is recorded by `apify_run_status`.
        _gratuit()
        return out

    # --- read the output ----------------------------------------------------

    @mcp.tool(annotations=LECTURE)
    def apify_dataset_items(
        dataset_id: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list[str]] = None,
        omit: Optional[list[str]] = None,
        clean: Optional[bool] = None,
    ) -> Any:
        """Read the results a run produced.

        Returns: the LIST of items.

        Args:
            dataset_id: the run's `defaultDatasetId`.
            fields / omit: keep or drop keys — worth using, several actors return
                objects with dozens of fields per item.
            clean: skip empty/hidden items.
        """
        with _upstream():
            out = _client().dataset_items(
                dataset_id, limit=limit, offset=offset, fields=fields,
                omit=omit, clean=clean)
        _gratuit()
        return out
