"""Runs — lifecycle verbs of a walkthrough (ADR 0017, rung 2).

`run_start` opens a run (mints a `run_id`, pushes it into the session state);
every tool call until `run_finish` is **attributed to that run** by the calllog
sink (server-side correlation, the agent threads nothing). A run with `guide`
= the execution of a named (repeatable) guide; without `guide` = a one-shot
(ad-hoc) run, same trace. Loading a guide remains `oto_procedure(op='get')`
(unchanged). Platform spine: loaded explicitly in `register_all`, outside the
activation gate.
"""
from __future__ import annotations

import asyncio
import logging

from fastmcp import Context, FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import deprecations, guide_run as dr, run_status

logger = logging.getLogger(__name__)

# SINGLE source of the vocabulary (ADR 0058-D5): the tool, its docstring — hence the schema
# the agent reads — and every surface that validates an outcome read the same list. It
# diverged from the prose of block A for months, which is the most discreet way
# of lying to an agent: the schema says one thing, the instruction another.
_OUTCOMES = run_status.OUTCOMES


def liberer_les_lignes_du_run(run_id: str) -> int | None:
    """Returns to the queue the rows this run was still holding — the third release
    path (#317), shared by `run_finish` and the REST close (oto#227). The count
    is ALWAYS returned (#633): `0` written, `None` if the database coughed (logged).
    Best-effort — releasing is a service, never a condition of closing. Sync:
    called outside the loop."""
    from .. import db
    try:
        return db.datastore_release_by_run(run_id)
    except Exception:  # noqa: BLE001
        logger.warning("releasing the rows of run %s failed (best-effort)", run_id)
        return None


async def _noter_sur_le_travail(outcome: str, note: str | None) -> None:
    """Pose l'issue sur le TRAVAIL hébergé dont le jeton fait cet appel
    (`db/issues_des_travaux.py`) : un `done` dont l'agent conclut `blocked` est un
    travail mort, et sans cette trace le médecin des agents ne le voit pas. Seulement
    quand l'instance compte les runs bloqués (`issues_des_travaux.compte_les_bloques`) ;
    hors travail hébergé (une personne, une session MCP), rien. Une écriture ratée LÈVE :
    l'appelant l'a placée après la clôture et la libération des lignes, qui sont faites."""
    from starlette.concurrency import run_in_threadpool
    from ..db import issues_des_travaux
    if not issues_des_travaux.compte_les_bloques():
        return
    from .. import verrou_org
    v = verrou_org.travail_de_la_requete()
    if v is None or v.job_id is None:
        return
    await run_in_threadpool(issues_des_travaux.noter_issue, int(v.job_id), v.sub,
                            outcome, note)


def _run_d_autrui(run_id: str) -> bool:
    """Is `run_id` a run indexed under ANOTHER account than the caller's? The guard of
    `run_finish`'s release (the REST close has its own, `run_thread._clore`).

    Same ownership rule as `db.finish_run`: `sub IS NOT DISTINCT FROM` — local stdio
    (no sub) owns the runs without a sub. A run absent from the index (`run_start`
    persists it best-effort) keeps the behaviour from before: released, nobody else
    can be proven to own it. An unreadable index fails CLOSED on the release only —
    the close itself never depends on it. Sync: called outside the loop."""
    from .. import db
    from ..auth.hooks import current_user_sub_from_token
    try:
        head = db.get_run_head(run_id)
        sub = current_user_sub_from_token()
    except Exception:  # noqa: BLE001
        logger.warning("owner of run %s unreadable: its rows are not released "
                       "(fail-closed)", run_id, exc_info=True)
        return True
    return head is not None and head.get("sub") != sub


def procedure_servie(sub: str, slug: str) -> tuple[dict, str] | None:
    """The procedure a read BY NAME serves this account, and the tier it lives at
    (`user`|`org`|`group`) — the copy a run executes, and the one a write must target.
    The order of `oto_procedure(op='get')`'s cascade: MY copy first (the default write
    scope, ADR 0068) when it is IN SERVICE — an archived personal copy masks nothing
    (#1156, decision of 14/10/2026) —, then the active org; the active team as a
    complement. None if the slug designates nothing readable here.

    DB read ⇒ called OUTSIDE the loop (`asyncio.to_thread`)."""
    from .. import access, org_store
    mine = org_store.get_instruction_en_service("user", str(sub), slug)
    if mine:
        return mine, "user"
    org_id = access.current_org(sub)
    if org_id is not None:
        row = org_store.get_instruction("org", org_id, slug)
        if row:
            return row, "org"
    gid = access.current_group(sub)
    if gid is not None:
        row = org_store.get_instruction("group", gid, slug)
        if row:
            return row, "group"
    return None


def empreinte_de_procedure(sub: str | None, slug: str) -> tuple[int, str] | None:
    """CURRENT version of the procedure `slug` and the TIER it was read at
    (`user`|`org`|`group`) — the copy a read by name serves, so the one the run
    executes (`procedure_servie`). None if the slug designates no procedure — an
    ad-hoc run, a guide from another household, or an invented slug.

    ⚠️ (#1156) It read the org, then the team, but NEVER my personal copy: a run that
    executed a personal copy masking the org's logged the ORG's version — a
    fingerprint of a text the run never walked through. The tier travels with the
    version: two tiers carry the same slug, and a version number alone does not say
    which one.

    DB read ⇒ called OUTSIDE the loop (`asyncio.to_thread`)."""
    if not sub:
        return None
    lu = procedure_servie(sub, slug)
    if lu is None or lu[0].get("version") is None:
        return None
    row, palier = lu
    return int(row["version"]), palier


async def _note_procedure_version(guide: str | None) -> int | None:
    """The run's FINGERPRINT: WHICH version of the procedure it executes.

    The journal column only carries a **slug**, while procedures are versioned
    (`org_instructions.version`, snapshot per version in `org_instruction_revisions`).
    A run therefore didn't record what it actually walked through: replaying "the same
    procedure" three weeks later means playing another one without knowing it.
    ADR 0055-D10 / 0058-D1: the version freeze IS the run's fingerprint.

    It lands in the JOURNAL, next to the slug typed by the agent — the call trace
    (`session_org.note_call_trace`, allowlist `server._TRACED_ARGS`) pours the value
    into the args of THIS `run_start` row. This is the home consistent with the
    verdict of 12/08: the run is its facts, not its index row.

    Best-effort, like everything around a run: an unavailable version never
    prevents a run from opening."""
    if not guide:
        return None
    try:
        from .. import session_org
        from ..auth.hooks import current_user_sub_from_token
        sub = current_user_sub_from_token()
        empreinte = await asyncio.to_thread(empreinte_de_procedure, sub, guide)
    except Exception:
        logger.warning("procedure version unavailable for %r (best-effort)",
                       guide, exc_info=True)
        return None
    if empreinte is None:
        return None
    version, palier = empreinte
    # The TIER executed rides along (`guide_scope`, declared in `server._TRACED_ARGS`).
    session_org.note_call_trace(doctrine_version=version, guide_scope=palier)
    return version


async def _persist_open(run_id: str, label: str, guide: str | None) -> None:
    """Durable trace of the opening (best-effort, off-loop). The session stack remains
    the source of the active run; this only adds label/guide in the database."""
    try:
        from .. import access, db
        from ..auth.hooks import current_user_sub_from_token
        sub = current_user_sub_from_token()
        org_id = access.current_org(sub) if sub else None
        project_id = access.current_project() if sub else None  # frozen active project (ADR 0032 B3)
        await asyncio.to_thread(
            db.insert_run, run_id, sub=sub, org_id=org_id, label=label,
            guide=guide, project_id=project_id)
    except Exception:
        logger.warning("run_start persistence failed for run_id=%s (best-effort)",
                       run_id, exc_info=True)


async def _persist_close(run_id: str, outcome: str, note: str | None) -> None:
    try:
        from .. import db
        from ..auth.hooks import current_user_sub_from_token
        # Scoped by sub: we only close OUR OWN run (someone else's run_id — reused
        # session, #108 — cannot be closed). No-op if run_id/sub don't match.
        await asyncio.to_thread(db.finish_run, run_id, outcome, note,
                                sub=current_user_sub_from_token())
    except Exception:
        logger.warning("run_finish persistence failed for run_id=%s (best-effort)",
                       run_id, exc_info=True)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def run_start(ctx: Context, label: str, guide: str | None = None,
                        doctrine: str | None = None) -> dict:
        """Open a run (a tracked walkthrough) so a procedure can be reviewed later.
        Returns a `run_id` — keep it and pass it to `run_finish` when you're done.
        Every tool call until then is automatically attributed to this run.

        Use it for a repeatable guide/skill (pass `guide`) AND for any one-shot
        procedure worth logging (omit `guide`).

        Returns `guide_version` — the version of that procedure frozen for this run
        (null for an ad-hoc run, or if the slug matches no procedure you can read).
        The response also carries the deprecated `doctrine`/`doctrine_version` keys
        until 29/10/2026.

        Args:
            label: short human description of what this run does (always logged).
            guide: optional — the guide/skill slug being executed (as passed to
                oto_procedure op=get). Omit for a one-shot/ad-hoc run.
            doctrine: DEPRECATED alias of `guide`, removed on 30/11/2026 — pass
                `guide` instead.
        """
        guide = guide if guide is not None else doctrine
        run_id = dr.new_run_id()
        await dr.push_run(ctx, run_id, label, guide)
        # run_id call axis (#108): set WITHOUT reset — the ContextVar dies with the
        # request, but stamps run_start's own tool_call under its run, and
        # primes the axis for the agent (who then passes it via `_run_id=`).
        from .. import session_org
        session_org.set_call_run(run_id)
        # The slug lands in the journal under THE key the run reconstruction and the
        # project audit read (#1340): the journal keeps the args as passed, and
        # `guide=` (the documented name) never wrote that key.
        from ..db import usage as db_usage
        session_org.note_call_trace(**{db_usage._ARG_PROCEDURE: guide})
        version = await _note_procedure_version(guide)
        await _persist_open(run_id, label, guide)
        return deprecations.avec_les_deux_noms(
            {"run_id": run_id, "label": label, "guide": guide,
             "guide_version": version})

    @mcp.tool()
    async def run_finish(
        ctx: Context, run_id: str, outcome: str, note: str | None = None,
    ) -> dict:
        """Close a run opened with `run_start`, with one `outcome`:

        - `done` — everything asked is done.
        - `partial` — the run ended cleanly but did only part of the work: `note` is
          REQUIRED and says what is done and what remains.
        - `blocked` — stopped by an obstacle it cannot lift alone (access, missing
          data, a cap, a human decision).
        - `failed` — an error broke the run.

        Whatever the outcome, the rows the run still held go back to the queue
        (`rows_released`).

        Args:
            run_id: the id returned by run_start.
            outcome: one of done | partial | failed | blocked.
            note: what worked, where it broke, what was missing — required for partial.
        """
        refus = run_status.refus_de_cloture(outcome, note)
        if refus:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=refus[1]))
        # The close belongs to the run it closes: without this stamp, `tool_calls.
        # run_id` stays NULL on this row (the `_run_id=` axis is not advertised on
        # the run verbs) and a run's timeline — `get_run`, which filters on the
        # column — never shows its own end, while the outcome is read from
        # that row. Same gesture as `run_start`, symmetric and without reset.
        from .. import session_org
        session_org.set_call_run(run_id)
        removed = await dr.pop_run(ctx, run_id)
        await _persist_close(run_id, outcome, note)
        # THIRD release path of the queue lock (#317): a run that
        # ends no longer works, hence holds nothing anymore — whatever its
        # outcome. This is the answer to the measured case: a vanished worker left its row
        # blocked until the lease expired, i.e. 18 days on the only
        # reserved row production had ever carried, with nobody seeing it.
        # Best-effort and OUTSIDE the loop: releasing is a service rendered, never a
        # condition of closing the run — a run must be able to close even if the
        # database coughs.
        # The count is ALWAYS written (#633): a fleet post tells "zero
        # rows returned" (0) apart from "nothing was attempted" (null — the database coughed, the
        # journal says so); an absent field said neither.
        # The release belongs to the run's OWNER, like the close itself (`_persist_close`,
        # #108) and the REST close (`run_thread._clore`): a run_id is a handle, not a
        # right — someone else's would otherwise hand that run's leases back to the
        # queue mid-work. Someone else's run releases nothing (`0`, nothing was ours).
        from starlette.concurrency import run_in_threadpool
        if await run_in_threadpool(_run_d_autrui, run_id):
            liberees = 0
        else:
            liberees = await run_in_threadpool(liberer_les_lignes_du_run, run_id)
        # Après la clôture ET la libération : une issue non posée lève sans retenir de
        # ligne ni laisser le run ouvert.
        await _noter_sur_le_travail(outcome, note)
        return {"ok": True, "run_id": run_id, "outcome": outcome,
                "was_open": removed is not None, "rows_released": liberees}
