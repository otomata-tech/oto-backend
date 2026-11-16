"""One step's call through `meta.executer_cible` — the seam `oto_call` and the recipes
use — and the verdict on what it returned; the collect loop of an asynchronous step."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from fastmcp import FastMCP

from ..mcp_errors import McpError
from .etape import Step, _bills, _payload

#: An asynchronous step polls for at most this long, first poll after `POLL_FIRST_S`.
POLL_BUDGET_S = 25.0
POLL_FIRST_S = 8.0
POLL_EVERY_S = 6.0
#: The wait between two reads — a seam, so that a test runs a long wait in no time.
_sleep = asyncio.sleep


async def _poll(mcp: FastMCP, sub, step: Step, submitted, deadline: float):
    """Collect an asynchronous step: (issue | None, collect args | None, finished)."""
    from ..tools import meta
    args = step.collect.args(submitted)
    if not args:
        return None, None, True
    tool = await meta.resoudre_outil(mcp, step.collect.tool)
    if tool is None:
        return None, args, False
    if step.poll is not None:
        # Its own cadence (`Step.poll`), inside the cascade's budget all the same.
        first, every, most = step.poll
        stop = min(time.monotonic() + most, deadline)
    else:
        first, every = POLL_FIRST_S, POLL_EVERY_S
        stop = min(time.monotonic() + POLL_BUDGET_S, deadline)
    await _sleep(max(0.0, min(first, stop - time.monotonic())))
    while True:
        issue = await meta.executer_cible(tool, sub, step.collect.tool,
                                          step.collect.tool, dict(args))
        if not issue.ok or issue.retenu or step.collect.done(_payload(issue)):
            return issue, args, True
        if time.monotonic() + every > stop:
            return issue, args, False
        await _sleep(every)


async def collect_once(mcp: FastMCP, sub, step: Step, args: dict):
    """One more read of a step left pending (free): its `IssueCible`, or None."""
    from ..tools import meta
    tool = await meta.resoudre_outil(mcp, step.collect.tool)
    if tool is None:
        return None
    try:
        return await meta.executer_cible(tool, sub, step.collect.tool,
                                         step.collect.tool, dict(args))
    except McpError:
        return None


async def _call(mcp: FastMCP, sub, tool_name: str, args: dict, attempt: dict):
    """One step's call: its `IssueCible`, or None after recording a skip."""
    from ..tools import meta
    tool = await meta.resoudre_outil(mcp, tool_name)
    if tool is None:
        attempt.update(status="skipped", code="unknown_tool",
                       message=f"`{tool_name}` is not mounted on this instance")
        return None
    try:
        return await meta.executer_cible(tool, sub, tool_name, tool_name, dict(args))
    except McpError as e:
        # A GUARD refusal (connector not activated for this org/team, outside the
        # hosted job's tool list, access level): the caller's fault for a direct call,
        # a skipped step for a waterfall.
        data = getattr(e.error, "data", None)
        code = data.get("code") if isinstance(data, dict) else None
        attempt.update(status="skipped", code=code or "refused",
                       message=(e.error.message or "")[:300])
        return None


#: A step refused because the caller's own account is not linked (measured: "Connect
#: your Linkedin account at … before using these tools").
_NOT_CONNECTED = re.compile(r"\bConnect your \w+ account\b", re.I)


def _judge(issue, attempt: dict) -> bool:
    """Records a non-usable outcome on `attempt`; True if the result can be read."""
    if not issue.ok and issue.code == "not_found":
        # The provider answered "no such person/company" with a 404: a miss.
        attempt.update(status="no_match")
        return False
    if not issue.ok and _NOT_CONNECTED.search(issue.message or ""):
        # The caller's OWN account is not linked (their LinkedIn, for the free step): not
        # a failure of the provider — the step is skipped, and the receipt says why.
        attempt.update(status="skipped", code="not_connected",
                       message=(issue.message or "")[:300])
        return False
    if not issue.ok:
        attempt.update(status="failed", code=issue.code,
                       message=(issue.message or "")[:300])
        return False
    if issue.retenu:
        attempt.update(status="withheld", code="redaction_withheld",
                       message="the org's redaction policy withheld this result")
        return False
    return True


async def _fanned(mcp: FastMCP, sub, step: Step, arg_sets: list[dict], inputs: dict,
                  deadline: float, attempt: dict, budget: dict,
                  want: Optional[int] = None):
    """The calls of a step that sends several (`Step.fan_out`), in turn: their results
    merged (`Step.merge`), or None after recording why nothing could be read.

    Each call sent is billed by its tool and counted against `max_paid_steps`; before
    each, the call's limits are checked again — `max_paid_steps`, the time budget, and
    `want` (the find already holds that many people: the rest would be paid for
    nothing). A failed call does not undo the ones before it: their results are read,
    and the receipt says where it stopped (`stopped`). At most `step.max_calls` are
    sent; the argument sets past it are named (`not_sent`)."""
    sets, extra = arg_sets[:step.max_calls], arg_sets[step.max_calls:]
    paid = _bills(step)
    results: list = []
    tried, sent, why, refusal = 0, 0, None, None
    for i, args in enumerate(sets):
        if i and want and _count(step.extract(step.merge(results), inputs)) >= want:
            why = "limit_reached"
            break
        if (paid and budget.get("max_paid_steps") is not None
                and budget.get("paid_steps", 0) >= budget["max_paid_steps"]):
            why = "max_paid_steps"
            break
        if time.monotonic() >= deadline:
            why = "time_budget"
            break
        part: dict = {}
        tried += 1
        issue = await _call(mcp, sub, step.tool, args, part)
        if issue is not None:
            sent += 1
            if paid:
                budget["paid_steps"] = budget.get("paid_steps", 0) + 1
        if issue is None or not _judge(issue, part):
            if part.get("status") == "no_match":
                results.append({})      # a 404 from the provider: nobody for this one
                continue
            refusal = part
            why = part.get("code") or part.get("status")
            break
        results.append(_payload(issue))
    attempt["calls"] = sent
    not_sent = [a.get("query", a) for a in sets[tried:] + extra]
    if not_sent:
        attempt["not_sent"] = not_sent
    if why:
        attempt["stopped"] = why
    if refusal is not None and (not results
                                or not step.extract(step.merge(results), inputs)):
        # Nothing found, and a search failed (or was refused, or withheld): the outcome
        # is that failure, not a miss — a `no_match` would be written on a table's row,
        # where a failure leaves it to retry.
        attempt.update({k: v for k, v in refusal.items() if k != "stopped"})
        return None
    if not results:
        if why in ("max_paid_steps", "time_budget"):
            attempt.update(status="skipped", code=why)
        return None
    return step.merge(results)


def _count(found) -> int:
    return int(found.get("count") or 0) if isinstance(found, dict) else 0
