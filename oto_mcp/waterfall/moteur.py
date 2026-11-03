"""The cascade: the first step that finds the value wins, then the completion steps;
one step run on its key, its limits and the call's time budget; the receipt."""
from __future__ import annotations

import time
from typing import Optional

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool

from ..auth.hooks import current_user_sub_from_token
from . import appel, ordre, qualification, vivier
from .etape import Step, _bills, _missing, _payload, _runnable
from .valeurs import _bad

#: The whole cascade answers within this — under the 45 s of the REST tool-invoke path
#: (`capabilities/tools_me.py`), the tightest bound a caller meets (MCP: 120 s). A step
#: that would start after it is skipped (`time_budget`).
DEADLINE_S = 40.0
#: A step that bounds its own call (`Step.bounded_by_deadline`) is given what is left of
#: the budget, at most `BOUNDED_MAX_S` (its client's own read bound), and is skipped
#: when less than `BOUNDED_MIN_S` is left: a lookup cut off may be billed with nothing
#: to show, one not started costs nothing.
BOUNDED_MIN_S = 3
BOUNDED_MAX_S = 60


def _receipt(kind, found, provider, value, attempts, source, ignored, pending) -> dict:
    out = {"found": found, "provider": provider, kind: value, "attempts": attempts,
           "order_from": source}
    if ignored:
        out["ignored_steps"] = ignored
    if pending:
        out["pending"] = pending
    return out


async def _run_step(mcp: FastMCP, sub, step: Step, mode: str, inputs: dict,
                    dry_run: bool, deadline: float, attempt: dict, pending: list,
                    budget: Optional[dict] = None):
    """Run ONE step and record what happened on `attempt`; return its find, or None."""
    if step.variants:
        if not _runnable(step, inputs):
            attempt.update(status="skipped", code="missing_input",
                           message=f"needs {_missing(step)}")
            return None
        tried: list[dict] = []
        for v in step.variants:
            if not _runnable(v, inputs):
                continue
            part = {"via": v.key, "tool": v.tool}
            tried.append(part)
            found = await _run_step(mcp, sub, v, mode, inputs, dry_run,
                                    deadline, part, pending, budget)
            if found is not None:
                attempt.update(status="found", via=v.key, tool=v.tool, tried=tried)
                return found
        last = tried[-1] if tried else {}
        attempt.update({k: v for k, v in last.items() if k not in ("via", "tool")},
                       tried=tried)
        if dry_run:
            attempt.update(status="planned", arguments=last.get("arguments"),
                           cost=step.cost, tried=tried)
        return None
    if not _runnable(step, inputs):
        attempt.update(status="skipped", code="missing_input",
                       message=f"needs {_missing(step)}")
        return None
    if ordre.own_key_missing(step, mode):
        attempt.update(status="skipped", code="own_key_required", message=(
            "this step needs your own key for this provider — the shared key does not "
            "serve it" if mode != "unknown" else "this step needs your own key for this "
            "provider, and which key would serve it could not be read"))
        return None
    if mode in ordre._NO_KEY:
        attempt.update(status="skipped", code="no_key", message=(
            "no usable key for this provider — connect yours on its card"
            if mode == "forbidden" else "the shared key's quota is used up for today"))
        return None
    budget = budget if budget is not None else {}
    args = step.build(inputs)
    # The caller's OWN limits — none by default: an Apify run's cost ceiling, passed to
    # Apify as is; and how many paid calls one lookup may make (free steps don't count).
    if step.tool == "apify_run_sync" and budget.get("apify_max_usd"):
        args = {**args, "max_total_charge_usd": budget["apify_max_usd"]}
    paid = _bills(step)
    if (paid and budget.get("max_paid_steps") is not None
            and budget.get("paid_steps", 0) >= budget["max_paid_steps"]):
        attempt.update(status="skipped", code="max_paid_steps",
                       message=f"the call's limit of {budget['max_paid_steps']} paid "
                               "step(s) is reached")
        return None
    if dry_run:
        attempt.update(status="planned", arguments=args, cost=step.cost)
        return None
    if time.monotonic() >= deadline:
        attempt.update(status="skipped", code="time_budget",
                       message=f"the waterfall's {int(DEADLINE_S)} s budget is spent")
        return None
    run_s = args.get("timeout_secs") if step.tool == "apify_run_sync" else None
    if run_s and time.monotonic() + run_s > deadline + 5:
        # An Apify run that cannot end inside the call: not started (nothing billed),
        # rather than cut off by the REST bound with nothing to show.
        attempt.update(status="skipped", code="time_budget",
                       message=f"a {run_s} s run does not fit in what is left of the call")
        return None
    if step.bounded_by_deadline:
        left = int(deadline - time.monotonic())
        if left < BOUNDED_MIN_S:
            attempt.update(status="skipped", code="time_budget",
                           message=f"{max(left, 0)} s left in the call: too little for "
                                   "this lookup to answer")
            return None
        args = {**args, "timeout_s": min(left, BOUNDED_MAX_S)}
    issue = await appel._call(mcp, sub, step.tool, args, attempt)
    if issue is not None and paid:
        budget["paid_steps"] = budget.get("paid_steps", 0) + 1
    if issue is None or not appel._judge(issue, attempt):
        return None
    result = _payload(issue)
    if ((step.reveal or (step.quota_only and step.pages)) and inputs.get("_pool")
            and isinstance(result, dict)):
        result = await vivier._every_free_page(mcp, sub, step, args, result, inputs,
                                               deadline, attempt)
    if step.collect is not None:
        collected, collect_args, finished = await appel._poll(mcp, sub, step, result,
                                                             deadline)
        if not finished and step.falls_through:
            # Not done within its wait (bounded by the cascade's budget): the cascade
            # goes on, and reads it once more at the end if time is left (`_late_reads`).
            attempt.update(status="pending", falls_through=True,
                           collect={"tool": step.collect.tool, "arguments": collect_args},
                           message="not finished within its wait — the cascade went "
                                   "on to the next providers")
            return None
        if not finished:
            collect = {"tool": step.collect.tool, "arguments": collect_args}
            attempt.update(status="pending", collect=collect,
                           message="submitted, not finished in time — collect it "
                                   "later with `collect`; the cascade stops here")
            pending.append({"provider": step.key, **collect})
            return None
        if collected is None:
            attempt.update(status="no_match")
            return None
        if not appel._judge(collected, attempt):
            return None
        result = _payload(collected)
    found = step.extract(result, inputs)
    if not found:
        attempt.update(status="no_match")
        return None
    attempt.update(status="found")
    return found


def _limits(max_paid_steps: Optional[int], apify_max_usd: Optional[float]) -> dict:
    """The caller's own limits, validated — none is set by default."""
    if max_paid_steps is not None and max_paid_steps < 1:
        raise _bad("`max_paid_steps` must be at least 1 — omit it for no limit.")
    if apify_max_usd is not None and apify_max_usd < 0.50:
        raise _bad("`apify_max_usd` must be at least 0.50: Apify refuses a lower ceiling "
                   "on a run. Omit it for the default ceiling of an Apify launch.")
    return {"max_paid_steps": max_paid_steps, "apify_max_usd": apify_max_usd}


async def _run_cascade(mcp: FastMCP, ns: str, kind: str, steps: tuple[Step, ...],
                       inputs: dict, providers_arg: Optional[list[str]],
                       dry_run: bool, limits: Optional[dict] = None,
                       qualify: Optional[dict] = None,
                       deadline_at: Optional[float] = None,
                       synchronous_only: bool = False) -> dict:
    """The cascade: the first step that finds the value wins. Then the COMPLETION
    steps (`Step.fills`, e.g. a company's LinkedIn page): each runs only while the
    value still lacks the field it fills, and merges what it finds — a page found
    when nothing else identified the company still makes a (thin) find.

    A step left `pending` (asynchronous, not done in time) STOPS the cascade: its answer
    is paid and still coming, so no later step — completion included — is paid for the
    same lookup; the receipt carries the collect call."""
    # The identity seam and the plan read the database: off the loop
    # (`docs/event-loop-perf.md`).
    sub = await run_in_threadpool(current_user_sub_from_token)
    order, ignored, source, modes = await run_in_threadpool(
        ordre._plan, ns, steps, providers_arg, sub)
    if synchronous_only:
        # Table mode: an asynchronous step would be paid now and answer after the
        # call has written its row — its find would be lost (`waterfall_lignes.py`).
        order = [s for s in order if s.collect is None]
    if not any(_runnable(s, inputs) for s in order):
        raise _bad(
            f"No provider of this {kind} waterfall can run on these inputs. Each needs: "
            + "; ".join(f"{s.key} → {_missing(s)}" for s in order) + ".")
    if qualify:
        # Asked for, and Jev cannot run for this caller: refused BEFORE any step is paid
        # — results that come back unjudged would read as judged ones.
        await run_in_threadpool(qualification._require_qualification, sub, ns)
    deadline = deadline_at or (time.monotonic() + DEADLINE_S)
    if kind == "people" and qualify:
        # Judging needs the whole free pool, not its first `limit`: kept up to
        # POOL_MAX until Jev has ranked it (what is PAID is still sized by `limit`).
        inputs = {**inputs, "_pool": vivier.POOL_MAX}
    attempts: list[dict] = []
    pending: list[dict] = []
    budget = dict(limits or {})
    tally = qualification._Tally()
    found, winner = None, None
    deja_juge = False
    fallback, fallback_step = None, None
    for step in order:
        if step.fills:
            continue
        if found is not None or pending:
            break       # the receipt ends at the winner — or at the step still coming
        attempt = {"provider": step.key, "tool": step.tool,
                   "key": modes.get(step.tool, "unknown")}
        attempts.append(attempt)
        got = await _run_step(mcp, sub, step, attempt["key"], inputs, dry_run,
                              deadline, attempt, pending, budget)
        if (got is not None and step.weak is not None and step.weak(got) and step.reveal
                and qualify and not dry_run):
            # Masked names, but titles: judged for free, then only the ones that pass
            # are revealed — instead of paying the next provider for everyone.
            revealed = await vivier._reveal_qualified(mcp, sub, step, got, inputs,
                                                      qualify, deadline, budget, tally,
                                                      attempt)
            if revealed is not None:
                if revealed["people"]:
                    found, winner, deja_juge = revealed, step.key, True
                    continue
                attempt.update(status="unqualified")
                continue
        if got is not None and step.weak is not None and step.weak(got):
            attempt.update(status="weak")
            if fallback is None:
                fallback, fallback_step = got, step.key
            continue
        if got is not None and not dry_run and qualification._per_step(kind, qualify):
            got, report = await qualification._qualify(mcp, sub, kind, got, inputs,
                                                       qualify, deadline, tally.seen())
            tally.add(attempt, report)
            if (report.get("status") == "done" and step.pages and _bills(step)
                    and len(got["people"]) < inputs["limit"]):
                got = await vivier._fill_from_pages(mcp, sub, step, got, inputs, qualify,
                                                    deadline, budget, tally, attempt)
            if report.get("status") == "done" and not got["people"]:
                attempt.update(status="unqualified")
                continue    # nobody here passes: the next provider is tried
        found = got
        winner = step.key if found is not None else None
    late_pending: list[dict] = []
    if not dry_run:
        found, winner = await _late_reads(mcp, sub, order, inputs, attempts, found,
                                          winner, late_pending, deadline)
    if found is None and fallback is not None:
        found, winner = fallback, fallback_step
        if not dry_run and qualification._per_step(kind, qualify):
            found, report = await qualification._qualify(mcp, sub, kind, found, inputs,
                                                          qualify, deadline, tally.seen())
            tally.add(None, report)
            if report.get("status") == "done" and not found["people"]:
                found, winner = None, None
    for step in order:
        if not step.fills or pending:
            continue
        if isinstance(found, dict) and found.get(step.fills):
            continue    # the field is there: nothing to fill, nothing to say
        if step.same_as and any(a["provider"] == step.same_as
                                and a.get("status") in ("found", "no_match")
                                for a in attempts):
            continue    # the same question was already answered
        attempt = {"provider": step.key, "tool": step.tool,
                   "key": modes.get(step.tool, "unknown"), "fills": step.fills}
        attempts.append(attempt)
        extra = await _run_step(mcp, sub, step, attempt["key"], inputs, dry_run,
                                deadline, attempt, pending, budget)
        if extra:
            found = {**(found or {}), **{k: v for k, v in extra.items()
                                          if not (found or {}).get(k)}}
            winner = winner or step.key
    if dry_run:
        return {"dry_run": True, "order_from": source, "attempts": attempts,
                **({"ignored_steps": ignored} if ignored else {}),
                **({"qualification": {"tool": "jev_items", "status": "planned"}}
                   if qualify else {})}
    if qualify and found and not qualification._per_step(kind, qualify) and not deja_juge:
        found, report = await qualification._qualify(mcp, sub, kind, found, inputs,
                                                      qualify, deadline)
        tally.add(None, report)
    if kind == "people" and found and len(found.get("people") or []) > inputs["limit"]:
        # The pool was widened for judging (`_pool`): the answer is `limit` people,
        # the best fit first when they were judged.
        people = found["people"]
        if any("qualification" in p for p in people):
            people = sorted(people, key=qualification._fit, reverse=True)
        found = {**found, "people": people[:inputs["limit"]],
                 "count": min(len(people), inputs["limit"])}
    out = _receipt(kind, found is not None, winner, found, attempts, source, ignored,
                   pending + late_pending)
    if tally.reports:
        out["qualification"] = tally.receipt(qualify)
        if (qualification._per_step(kind, qualify) and found
                and found["count"] < inputs["limit"]):
            _continue_with(out, order, winner, inputs, found["count"])
        if tally.rejected:
            # ALWAYS returned (a few tokens each): checking the rubric never costs a
            # second, billed cascade. The full records on demand.
            out["unqualified"] = tally.rejected if qualify.get("show") else tally.compact()
    return out


async def _late_reads(mcp: FastMCP, sub, order: list[Step], inputs: dict,
                      attempts: list[dict], found, winner, late_pending: list[dict],
                      deadline: float):
    """The steps that fell through still pending (`Step.falls_through`): read ONCE more,
    free, when the cascade ends — even when another provider found: the read is what
    settles the lookup at what it found (its tool notes it), where a lookup never read
    would be settled at its worst case when its hold expires. Its find answers if
    nothing else did (`found_late` otherwise: billed by its provider all the same).
    Still running, or no time left in the call's budget: it stays in the receipt's
    `pending`, with the call that collects it."""
    by_key = {s.key: s for s in order}
    for attempt in attempts:
        if not attempt.get("falls_through") or attempt.get("status") != "pending":
            continue
        step, collect = by_key[attempt["provider"]], attempt["collect"]
        if time.monotonic() >= deadline:
            late_pending.append({"provider": step.key, **collect})
            continue
        issue = await appel.collect_once(mcp, sub, step, collect["arguments"])
        if issue is None or not issue.ok or issue.retenu \
                or not step.collect.done(_payload(issue)):
            late_pending.append({"provider": step.key, **collect})
            continue
        attempt.pop("collect", None)
        late = step.extract(_payload(issue), inputs)
        if not late:
            attempt.update(status="no_match", message="finished after the fallback")
        elif found is None:
            attempt.update(status="found", message="finished after the fallback")
            found, winner = late, step.key
        else:
            attempt.update(status="found_late", found=late,
                           message="finished after another provider answered — "
                                   "billed by its provider all the same")
    return found, winner


def _continue_with(out: dict, order: list[Step], winner: Optional[str], inputs: dict,
                   count: int) -> None:
    """Fewer qualified people than asked: the cascade STOPS (stop-at-first-hit), and the
    calling agent decides whether the rest is worth paying for."""
    if not winner:
        return
    after = order[[s.key for s in order].index(winner) + 1:]
    rest = [s.key for s in after if not s.fills and _runnable(s, inputs)]
    if rest:
        out["continue_with"] = {
            "providers": rest, "short_of_limit": inputs["limit"] - count,
            "message": (f"{count} qualified of the {inputs['limit']} asked. To look for "
                        "more, call again with these `providers` (each is billed); "
                        "already-found people may come back.")}
