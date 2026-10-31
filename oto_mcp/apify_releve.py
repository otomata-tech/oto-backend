"""Settling the Apify runs nobody read back, and checking nothing else slipped through.

A run outlives its call: at 300 s `apify_run_sync` hands back and the run goes on, and
only a read of the FINISHED run traces its cost. A run nobody reads costs the platform
and bills nobody. So a platform-key run is recorded at launch (`db/apify_runs.py`), and
this daily job does the rest, in two independent steps:

1. **Settle.** Each run without a cost is read on the platform key. A finished one gets
   a journal line shaped like the `apify_run_status` that read it — tool, `quantity` in
   micro-dollars, `apify_run_id` — under the org and person who LAUNCHED it. The billing
   lens reads that line like any other, and the biller keeps one line per job: a late
   read by the launcher changes nothing.
2. **Compare.** Apify's own count for the usage cycle against what the table settled.
   This does not depend on the recording: it is what shows the next leak, whatever its
   shape. A gap is a signal for an operator, never a bill. Runs started from Apify's
   console, and whatever was spent before the table's first run, are not ours to
   compare: the check waits for a whole cycle.

Run by `oto-mcp maintenance apify-runs`, prod only (the database is shared).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import credentials_store
from .calllog import truncated_args
from .db import apify_runs, usage

logger = logging.getLogger(__name__)

#: A run still unfinished after this long is left to the comparison, and said.
_ABANDON_APRES = timedelta(days=7)
#: A gap below this is Apify's own storage and rounding, not a leak.
SEUIL_ECART_USD = 5.0
_CIBLE_SIGNAL = "apify_cost_gap"
_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"


def _instant(valeur: Any) -> datetime:
    """A date as the store serves it: a string, UTC, without its zone (`_str_dict_row`)."""
    lue = (valeur if isinstance(valeur, datetime)
           else datetime.fromisoformat(str(valeur).replace("Z", "+00:00")))
    return lue if lue.tzinfo else lue.replace(tzinfo=timezone.utc)


def _cle_plateforme() -> Optional[str]:
    """The platform key, or None when this instance has none."""
    lignes = credentials_store.list_platform_credentials("apify")
    if not lignes:
        return None
    ligne = credentials_store.get_credential_with_meta(
        credentials_store.PLATFORM, lignes[0]["label"], "apify", "")
    if not ligne:
        raise RuntimeError("the Apify platform row no longer resolves")
    return credentials_store.unpack_secret("apify", ligne["secret"])["key"]


def _tracer(ligne: dict, cout: float) -> None:
    """The billed line of a settled run, under its launcher. `_microdollars` and the
    argument name are the ones `apify_run_status` writes: the biller cannot tell them
    apart, which is the point."""
    from .tools.apify import _microdollars
    usage.insert_tool_call({
        "tool": "apify_run_status", "sub": ligne["sub"], "org_id": ligne["org_id"],
        "run_id": ligne["run_id"], "ok": True, "duration_ms": 0,
        "quantity": _microdollars(cout), "key_mode": "platform",
        "args": truncated_args({"apify_run_id": ligne["apify_run_id"], "releve_serveur": True}),
    })


def _relever_un(client: Any, ligne: dict, *, dry_run: bool, maintenant: datetime) -> str:
    from oto.tools.common.errors import UpstreamHTTPError

    from .tools.apify import _TERMINAL, _cout, _run_of
    run_id = ligne["apify_run_id"]
    try:
        run = _run_of(client.run_status(run_id))
    except UpstreamHTTPError as e:
        if e.status_code != 404:
            raise
        logger.warning("apify run %s: unknown to Apify, no cost to settle", run_id)
        if not dry_run:
            apify_runs.marquer(run_id, statut="introuvable", cout_usd=None)
        return "introuvables"
    statut = run.get("status") if run else None
    if statut not in _TERMINAL:
        if maintenant - _instant(ligne["lance_le"]) > _ABANDON_APRES:
            logger.warning("apify run %s: %s after %s, still unsettled", run_id, statut,
                           _ABANDON_APRES)
        return "en_cours"
    cout = _cout(run, run_id, "apify_run_status")
    if cout is None:
        logger.warning("apify run %s: finished (%s) without a readable cost", run_id, statut)
        return "illisibles"
    if not dry_run:
        if cout > 0:
            _tracer(ligne, cout)
        apify_runs.marquer(run_id, statut=statut, cout_usd=cout)
    return "releves"


def relever(cle: str, *, dry_run: bool = False) -> dict:
    """Settle the runs without a cost. One failed read leaves that run for tomorrow."""
    from oto.tools.apify.client import ApifyClient
    client = ApifyClient(api_key=cle)
    maintenant = datetime.now(timezone.utc)
    compte = {"releves": 0, "en_cours": 0, "illisibles": 0, "introuvables": 0, "echecs": 0}
    for ligne in apify_runs.a_relever():
        try:
            compte[_relever_un(client, ligne, dry_run=dry_run, maintenant=maintenant)] += 1
        except Exception:
            logger.warning("apify run %s: settlement failed, retried tomorrow",
                           ligne["apify_run_id"], exc_info=True)
            compte["echecs"] += 1
    if not dry_run:
        compte["purges"] = apify_runs.purger()
    return compte


def _usage_du_cycle(cle: str) -> tuple[str, float]:
    """`(cycle start, dollars)` as Apify counts them — the probe's endpoint."""
    import requests
    r = requests.get(_LIMITS_URL, headers={"Authorization": f"Bearer {cle}"}, timeout=15)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    return data["monthlyUsageCycle"]["startAt"], float(data["current"]["monthlyUsageUsd"])


def comparer(cle: str, *, dry_run: bool = False) -> dict:
    """Apify's count for the cycle against what was settled."""
    debut, apify_usd = _usage_du_cycle(cle)
    bilan = apify_runs.bilan_depuis(debut)
    out = {"cycle_depuis": debut, "apify_usd": round(apify_usd, 2),
           "releve_usd": round(bilan["releve_usd"], 2), "en_attente": bilan["en_attente"]}
    premier = bilan["premier"]
    if premier is None or _instant(premier) > _instant(debut):
        return {**out, "ecart_usd": None,
                "note": "the table does not cover this whole cycle yet"}
    ecart = round(apify_usd - bilan["releve_usd"], 2)
    out["ecart_usd"] = ecart
    if ecart >= SEUIL_ECART_USD:
        corps = (f"Apify counts ${apify_usd:.2f} since {debut}, ${bilan['releve_usd']:.2f} "
                 f"settled, ${ecart:.2f} unexplained ({bilan['en_attente']} runs still "
                 "without a cost). Look for runs launched outside the recorded paths.")
        logger.warning("apify cost gap: %s", corps)
        if not dry_run:
            usage.insert_usage_signal(
                sub=None, org_id=None, signal="gap", kind="billing",
                target=_CIBLE_SIGNAL, body=corps, session_id=None, source="maintenance")
            out["signale"] = True
    return out


def maintenance(*, dry_run: bool = False) -> dict:
    cle = _cle_plateforme()
    if cle is None:
        return {"cle_plateforme": False}
    return {"relevement": relever(cle, dry_run=dry_run),
            "comparaison": comparer(cle, dry_run=dry_run)}
