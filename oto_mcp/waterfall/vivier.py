"""The people pool of `waterfall_people`: every page of a free search, the next pages of
a paid list, the reveals of only those Jev keeps — and the default, slim answer."""
from __future__ import annotations

import time
from typing import Optional

from fastmcp import FastMCP

from . import appel, qualification
from .etape import Step, _payload
from .qualification import _QUALIFY_MIN_S, _Tally
from .valeurs import _get, _person_key

#: The most people a judged pool holds: every page of a free search, up to this (Jev
#: judges them 200 a call — 1,000 titles are ~$0.02).
POOL_MAX = 1000
#: Seconds kept, after the pages, for Jev and the reveals.
_PAGES_RESERVE_S = _QUALIFY_MIN_S + 8.0


async def _every_free_page(mcp: FastMCP, sub, step: Step, args: dict, first: dict,
                           inputs: dict, deadline: float, attempt: dict) -> dict:
    """EVERY page of a FREE search (Apollo's people search: names masked, nothing billed;
    lemlist's database) — the whole pool goes to Jev, not its first page. Each page is
    reduced by `step.slim` (when it has one) BEFORE it joins the pool. Stops at the last
    page, at `_pool`, or when the time Jev and the reveals need is all that is left; the
    receipt says which (`pages`, `total`, `pool_truncated`)."""
    rows = _rows_key(first)
    raw = list(first.get(rows) or [])
    total = (first.get("total_entries") or _get(first, "pagination.total_entries")
             or first.get("total") or 0)
    param, page = step.pages or ("page", 1)
    per = int(args.get("per_page") or args.get("size") or len(raw) or 1)
    people = _slimmed(step, raw, inputs)
    why = None
    read = 1
    while total and read * per < total:
        if len(people) >= inputs["_pool"]:
            why = "pool_max"
            break
        if time.monotonic() > deadline - _PAGES_RESERVE_S:
            why = "time_budget"
            break
        page += 1
        part: dict = {}
        issue = await appel._call(mcp, sub, step.tool, {**args, param: page}, part)
        if issue is None or not appel._judge(issue, part):
            why = part.get("code") or "page_refused"
            break
        read += 1
        more = _get(_payload(issue), rows) or []
        if not more:
            break
        people += _slimmed(step, more, inputs)
    attempt.update(pages=read, total=total)
    if why:
        attempt["pool_truncated"] = why
    return {**first, rows: people}


def _slimmed(step: Step, rows: list, inputs: dict) -> list:
    """One page's rows as the pool keeps them: reduced by the step's `slim`, if any."""
    return step.slim(rows, inputs) if step.slim else list(rows)


def _rows_key(payload: dict) -> str:
    """Where a free search puts its rows: `people` (Apollo), `results` (lemlist)."""
    return next((k for k in ("people", "results") if isinstance(payload.get(k), list)),
                "people")


async def _fill_from_pages(mcp: FastMCP, sub, step: Step, got: dict, inputs: dict,
                           qualify: dict, deadline: float, budget: dict,
                           tally: _Tally, attempt: dict) -> dict:
    """A PAID list that came up short of `limit` qualified people: its NEXT pages are
    bought one at a time, each judged as it comes, until `limit` pass — or the results
    end, a page brings nobody new, the time Jev needs is all that is left, or
    `max_paid_steps` is reached (each page is a paid step). The receipt says how many
    pages and why it stopped (`pages`, `stopped`)."""
    param, first = step.pages
    base = step.build(inputs)
    # A fanned step (Google: one search per role) buys the next page of EACH search.
    bases = step.fan_out(inputs) if step.fan_out is not None else None
    people = list(got["people"])
    seen = {_person_key(p) for p in people} | set(tally.seen())
    page, read, why = first + 1, 1, None
    while len(people) < inputs["limit"]:
        if (budget.get("max_paid_steps") is not None
                and budget.get("paid_steps", 0) >= budget["max_paid_steps"]):
            why = "max_paid_steps"
            break
        if time.monotonic() > deadline - _PAGES_RESERVE_S:
            why = "time_budget"
            break
        part: dict = {}
        if bases is not None:
            payload = await appel._fanned(mcp, sub, step,
                                          [{**b, param: page} for b in bases], inputs,
                                          deadline, part, budget)
            if payload is None:
                why = part.get("code") or "page_refused"
                break
        else:
            issue = await appel._call(mcp, sub, step.tool, {**base, param: page}, part)
            if issue is not None:
                budget["paid_steps"] = budget.get("paid_steps", 0) + 1
            if issue is None or not appel._judge(issue, part):
                why = part.get("code") or "page_refused"
                break
            payload = _payload(issue)
        read += 1
        more = step.extract(payload, inputs) or {}
        fresh = [p for p in more.get("people") or [] if _person_key(p) not in seen]
        if not fresh:
            break       # the end of the results, or only people already seen
        seen |= {_person_key(p) for p in fresh}
        judged, report = await qualification._qualify(
            mcp, sub, "people", {"people": fresh, "count": len(fresh)}, inputs, qualify,
            deadline, tally.seen())
        tally.add(None, report)
        if report.get("status") != "done":
            why = report.get("code") or "qualification_skipped"
            break
        attempt["qualified"] = (attempt.get("qualified") or 0) + report.get("qualified", 0)
        attempt["not_qualified"] = ((attempt.get("not_qualified") or 0)
                                    + report.get("not_qualified", 0))
        people += judged["people"]
        page += 1
    attempt["pages"] = read
    if why:
        attempt["stopped"] = why
    return {**got, "people": people, "count": len(people)}


#: What a person keeps by default in `waterfall_people`'s answer (`full=True`: all).
_PERSON_KEYS = ("full_name", "title", "linkedin_url", "location", "seniority", "email",
                "email_status", "apollo_id", "name_obfuscated", "has_email",
                "has_direct_phone")


def _slim_person(p: dict) -> dict:
    out = {k: p[k] for k in _PERSON_KEYS if p.get(k) not in (None, "", [])}
    answers = (p.get("qualification") or {}).get("answers") or {}
    if answers:
        # The answers as their values (`{"fit": 0.82}`): the model is said once, on the
        # receipt's `qualification`, not on every person.
        out["fit"] = {n: a.get("noul", a.get("choice", a.get("score")))
                      for n, a in answers.items() if isinstance(a, dict)}
        passed = (p.get("qualification") or {}).get("passed")
        if passed is not None:
            out["passed"] = passed
    return out


def _slim_people(out: dict) -> dict:
    """The default answer of `waterfall_people`: each person reduced to who they are,
    how to reach them and Jev's verdict — the rest (a source's raw extras, the model
    repeated per person) stays behind `full=True`."""
    out = dict(out)
    if isinstance(out.get("people"), dict):
        out["people"] = {**out["people"], "people": [
            _slim_person(p) for p in out["people"].get("people") or []
            if isinstance(p, dict)]}
    # `unqualified` is left as asked: compact by default, whole with `show_unqualified`.
    return out


def _revealed_person(res, base: dict) -> Optional[dict]:
    """A revealed Apollo person, merged over what the search gave (its answers kept)."""
    p = _get(res, "person") or {}
    if not p or p.get("_stub") or not (p.get("name") or p.get("linkedin_url")):
        return None
    out = {k: v for k, v in base.items()
           if k not in ("name_obfuscated", "has_email", "has_direct_phone")}
    out.update({k: v for k, v in {
        "full_name": p.get("name"), "title": p.get("title") or base.get("title"),
        "linkedin_url": p.get("linkedin_url"), "seniority": p.get("seniority"),
        "location": ", ".join(x for x in (p.get("city"), p.get("country")) if x) or None,
        # Bought with the reveal, not extra: Apollo's email and its own status.
        "email": p.get("email"), "email_status": p.get("email_status"),
    }.items() if v})
    return out


async def _reveal_qualified(mcp: FastMCP, sub, step: Step, got: dict, inputs: dict,
                            qualify: dict, deadline: float, budget: dict,
                            tally: _Tally, attempt: dict) -> Optional[dict]:
    """Judge a free, masked pool with Jev, then reveal only the best `limit` that pass —
    each reveal one paid call (`step.reveal`). None when Jev could not judge (a refusal,
    no time): the pool then stays the cascade's fallback, as without `qualify`."""
    judged, report = await qualification._qualify(mcp, sub, "people", got, inputs,
                                                  qualify, deadline, tally.seen())
    tally.add(attempt, report)
    if report.get("status") != "done":
        return None
    best = sorted(judged["people"], key=qualification._fit,
                  reverse=True)[:inputs["limit"]]
    out, paid = [], 0
    for p in best:
        if not p.get("apollo_id"):
            continue
        if (budget.get("max_paid_steps") is not None
                and budget.get("paid_steps", 0) >= budget["max_paid_steps"]):
            attempt["stopped"] = "max_paid_steps"
            break
        if time.monotonic() >= deadline:
            attempt["stopped"] = "time_budget"
            break
        sub_attempt: dict = {}
        issue = await appel._call(mcp, sub, step.reveal, {"person_id": p["apollo_id"]},
                                  sub_attempt)
        if issue is not None:
            paid += 1
            budget["paid_steps"] = budget.get("paid_steps", 0) + 1
        if issue is None or not appel._judge(issue, sub_attempt):
            continue
        person = _revealed_person(_payload(issue), p)
        if person:
            out.append(person)
    attempt.update(status="found" if out else "unqualified", via=step.reveal,
                   revealed=len(out), reveals_paid=paid)
    return {"people": out, "count": len(out)}
