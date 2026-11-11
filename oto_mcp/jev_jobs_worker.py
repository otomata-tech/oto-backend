"""`jev_rows` background jobs (`jev_rows(background=true)`) — the loop that runs them.

A call judges what fits in its REST window (~100 rows); a job takes the whole table,
slice by slice, here, with no client connection to lose. Each tick claims the job that
waited longest and runs ONE slice of it (jobs take turns), then writes the slice's
counts and resume point back on the job.

**A slice runs AS ITS CALLER.** The sub, the org, the project and the run of the call
that queued the job are set again for every slice (`auth.hooks.sub_override`, the same
seam as « test a tool »), and `tools.jev.preparer` is called again under them: the write
right on the table, the shared key (a revoked grant), the model and the rubric are
re-checked before anything is sent, and every page re-resolves the key for its quota
(`client_partage(units=…)`). A paused account, a disabled tenant, a suspended org or a
member who left stops the job before its next slice.

**Billed per slice, in the same ledger as a call.** A slice is written to `tool_calls`
as a `jev_rows` MCP call (`kind='mcp'`, its own `call_uid`, the job's `run_id`), with the
spend and the key mode its trace collected — what the usage service reads. Per slice,
not per job: a credit cap must see the spend while the job runs.

**Resumable by construction.** Only undecided rows are taken (`model_column` empty), and
the cursor is written after every slice: a process killed mid-slice loses its lease,
and the next tick takes over from the last cursor. With `overwrite=true` that cursor is
what keeps rows from being judged (and billed) twice.

Acts on a third party (OpenRouter spends on the shared key): `tiers=True`, the
production drains the queue, preprod included (`boucles_de_fond.py`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

_POLL_S = 5
#: Send window of one slice; answers in flight when it closes still land.
TRANCHE_S = 90.0
#: Lease of a slice: its window + the worst in-flight call (10 + 15 s) + reads and
#: writes, with margin. Past it, another process may take the job over.
BAIL_S = 180
#: Consecutive slices that moved nothing before the job stops.
MAX_STALLED = 3


def _message(e: BaseException) -> str:
    err = getattr(e, "error", None)
    return str(getattr(err, "message", None) or e)


def _gardes(job: dict) -> Optional[str]:
    """Why the job can no longer run under its caller, or None."""
    from . import garde_identite, org_suspension, roles
    coupe = garde_identite.refus(job["sub"])
    if coupe:
        return coupe[1]
    org = job.get("org_id")
    if org is not None:
        if (suspendue := org_suspension.refus(int(org))):
            return suspendue
        if not roles.is_org_member(job["sub"], int(org)):
            return "the account that launched the job is no longer a member of its org."
    return None


def _facturer(job: dict, trace: dict, geste_id: str, debut: float,
              erreur: Optional[str]) -> None:
    """The slice in the call ledger, as a `jev_rows` call: what billing reads.
    A slice that spent is `ok` even when it stopped: the spend is real."""
    from . import db
    from .calllog import truncated_args
    quantite = trace.get("quantity")
    row = {
        "server": "oto", "kind": "mcp", "sub": job["sub"], "tool": "jev_rows",
        # `jev_job_id`, not `job_id`: the billing lens bills a job named under one of
        # its keys (`db/usage.py::BILLABLE_JOB_ARGS`) only ONCE, and each slice is a
        # spend of its own.
        "args": truncated_args({"jev_job_id": int(job["id"]), "background": True},
                               tool="jev_rows"),
        "ok": bool(quantite) or erreur is None,
        "error": (erreur or None) and erreur[:2000],
        "duration_ms": int((time.monotonic() - debut) * 1000),
        "run_id": job.get("run_id"), "org_id": job.get("org_id"),
        "call_uid": geste_id, "quantity": quantite, "key_mode": trace.get("key_mode"),
    }
    try:
        db.insert_tool_call(row)
    except Exception:  # noqa: BLE001 — logged; the slice's rows are written either way
        logger.error("jev_jobs_worker: job #%s slice NOT recorded in the ledger "
                     "(quantity=%s)", job["id"], quantite, exc_info=True)


def _tranche(job: dict) -> str:
    """One slice of `job`, under its caller. Returns the job's status afterwards.
    Never raises: every outcome is written on the job."""
    from . import access, geste, session_org
    from .access import credits as budget
    from .auth import hooks as auth_hooks
    from .db import jev_jobs as db_jev
    from .mcp_errors import McpError
    from .tools import jev
    from .tools import jev_rows as jr

    jid = int(job["id"])
    if job.get("cancel_requested"):
        db_jev.finish_jev_job(jid, "cancelled")
        return "cancelled"
    refus = _gardes(job)
    if refus:
        db_jev.finish_jev_job(jid, "failed", error=f"Stopped: {refus}")
        return "failed"

    trace: dict = {}
    geste_id = geste.nouvel_identifiant()
    debut = time.monotonic()
    out = panne = erreur = None
    cursor = None if job.get("final_pass") else job.get("cursor")
    try:
        with auth_hooks.sub_override(job["sub"]), \
                geste.portee(geste.AGENT, job["sub"], geste_id, outil="jev_rows"):
            axes = [(session_org.reset_call_trace, session_org.set_call_trace(trace))]
            echec: Optional[BaseException] = None
            try:
                budget.ouvrir_appel("jev_rows", job["params"], hors_borne=True)
                if job.get("org_id") is not None:
                    axes.append((session_org.reset_call_org,
                                 session_org.set_call_org(int(job["org_id"]))))
                if job.get("project_id") is not None:
                    axes.append((session_org.reset_call_project,
                                 session_org.set_call_project(int(job["project_id"]))))
                if job.get("run_id"):
                    axes.append((session_org.reset_call_run,
                                 session_org.set_call_run(job["run_id"])))
                plan = jev.preparer(job["adresse"], dry_run=False, **job["params"])
                out, panne = jev.qualifier(plan, cursor=cursor, limite=jr.MAX_ROWS,
                                           fenetre_s=TRANCHE_S, relever=jev.releve,
                                           passer_les_echecs=True)
                # The slice's settlement (L8), under its axes: this worker sets the
                # call record, so it is the edge that settles it — the success, or the
                # upstream refusal that stopped the batch.
                if panne is None:
                    access.regler_appel(job["sub"], "jev_rows", trace)
                else:
                    echec = panne
            except Exception as e:
                echec = e
                raise
            finally:
                if echec is not None:
                    access.regler_echec(job["sub"], "jev_rows", trace, echec)
                for reset, jeton in reversed(axes):
                    reset(jeton)
    except McpError as e:
        erreur = _message(e)
    except Exception as e:  # noqa: BLE001 — written on the job, the loop goes on
        logger.warning("jev_jobs_worker: job #%s slice failed", jid, exc_info=True)
        erreur = f"internal error ({type(e).__name__}); relaunch to resume."
    if panne is not None:
        erreur = _message(jev._refus_de_panne(panne, out["decided"]))
    _facturer(job, trace, geste_id, debut, erreur)

    if out is None:
        db_jev.finish_jev_job(jid, "failed", error=erreur)
        return "failed"

    final_pass = bool(job.get("final_pass"))
    termine = False
    if panne is None:
        if out.get("done"):
            termine = True
        elif out.get("_epuise"):
            # The pass read to the end of the table: once more from the top for rows
            # skipped (leased, changed, not sent), then stop. With `overwrite` a second
            # pass would judge everything again: the job ends here.
            if final_pass or job["params"].get("overwrite"):
                termine = True
            else:
                final_pass, cursor = True, None
        else:
            cursor = out.get("next_cursor")
    bouge = bool(out.get("decided") or out.get("jev_errors") or out.get("empty_state")
                 or out.get("skipped_leased") or out.get("skipped_changed")
                 or out.get("_epuise") or cursor != (None if job.get("final_pass")
                                                     else job.get("cursor")))
    etat = db_jev.record_jev_job_slice(
        jid, counters={k: out.get(k) or 0 for k in (
            "decided", "jev_errors", "empty_state", "skipped_leased", "skipped_changed",
            "low_confidence", "thin_state")},
        errors=out.get("errors") or [], error_count=out.get("error_count") or 0,
        cost=out.get("cost") or 0.0, cursor=cursor, final_pass=final_pass,
        remaining=out.get("remaining"), model=out.get("model"), stalled=not bouge)
    if panne is not None:
        db_jev.finish_jev_job(jid, "failed", error=erreur)
        return "failed"
    if etat.get("cancel_requested"):
        db_jev.finish_jev_job(jid, "cancelled")
        return "cancelled"
    if termine:
        db_jev.finish_jev_job(jid, "done")
        return "done"
    if int(etat.get("stalled") or 0) >= MAX_STALLED:
        codes = sorted({e.get("code") for e in out.get("errors") or [] if e.get("code")})
        db_jev.finish_jev_job(jid, "failed", error=(
            f"Stopped: {MAX_STALLED} slices in a row judged nothing"
            + (f" (upstream: {', '.join(codes)})" if codes else "")
            + ". Relaunch later to resume."))
        return "failed"
    return "running"


def _un_tour() -> int:
    """One SYNC tick (threadpool): claim and run AT MOST one slice. 1 if one ran."""
    from .db import jev_jobs as db_jev
    job = db_jev.claim_next_jev_job(BAIL_S)
    if job is None:
        return 0
    try:
        _tranche(job)
    except Exception as e:  # noqa: BLE001 — belt: the loop survives whatever happens
        logger.warning("jev_jobs_worker: job #%s blew up: %s", job.get("id"), e)
        try:
            db_jev.finish_jev_job(int(job["id"]), "failed",
                                  error=f"internal error ({type(e).__name__}).")
        except Exception:  # noqa: SILENT — the DB is unreachable, already logged above
            pass
    return 1


async def run_jev_jobs_loop(interval: int = _POLL_S) -> None:
    logger.info("jev_jobs_worker: started (poll %ss).", interval)
    while True:
        try:
            if await run_in_threadpool(_un_tour):
                continue          # more work may be waiting: no sleep between slices
        except Exception as e:  # noqa: BLE001 — a failed tick doesn't kill the loop
            logger.warning("jev_jobs_worker: tick failed: %s", e)
        await asyncio.sleep(interval)
