"""Qualification by Jev: a typed verdict per result, instead of an agent reading them —
optional (`qualify=`), on company, person and people; refused BEFORE anything is spent
when Jev cannot run for the caller."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastmcp import FastMCP

from . import appel, ordre
from .etape import _payload
from .valeurs import _bad, _person_key, _refused

#: The ONLY fields sent to Jev (a third party: OpenRouter, then TypeSafe) — what a fit
#: judgement needs, never a name, an email, a phone or a profile URL.
_QUALIFY_FIELDS = {
    "people": ("title", "headline", "seniority", "location"),
    "person": ("title", "headline", "seniority", "location", "company", "company_domain"),
    "company": ("name", "domain", "industry", "description", "employees", "revenue_range",
                "keywords", "country", "city", "founded"),
}
#: Under this many seconds left, the pass is skipped rather than cut off midway.
_QUALIFY_MIN_S = 5.0
#: States per `jev_items` call (its own maximum, `tools/jev.MAX_ITEMS`).
_JEV_CHUNK = 200


def _qualify_args(qualify, qualify_min, show_unqualified: bool = False) -> Optional[dict]:
    """The caller's rubric, validated — `None` when no qualification is asked."""
    if qualify is None:
        if qualify_min is not None or show_unqualified:
            raise _bad("`qualify_min` / `show_unqualified` need `qualify` (the rubric).")
        return None
    if not isinstance(qualify, dict) or not qualify:
        raise _bad("`qualify`: a Jev rubric `{name: {type, instructions, criteria}}`, "
                   "as `jev_ask` takes it.")
    if qualify_min is not None:
        if not 0 < qualify_min < 1:
            raise _bad("`qualify_min` is a probability, between 0 and 1 (e.g. 0.7).")
        if not any(isinstance(q, dict) and q.get("type") == "noul" for q in qualify.values()):
            raise _bad("`qualify_min` filters on the rubric's yes/no (`noul`) questions: "
                       "the rubric has none.")
    return {"questions": qualify, "min": qualify_min, "show": bool(show_unqualified)}


def _per_step(kind: str, qualify: Optional[dict]) -> bool:
    """People with a threshold are qualified STEP BY STEP: a provider whose people all
    fall short does not end the cascade, the next one is tried. A company or a person is
    judged once at the end — the next provider would only find the same one again."""
    return bool(qualify) and qualify.get("min") is not None and kind == "people"


class _Tally:
    """What qualification did over a whole cascade — the receipt's `qualification`."""

    def __init__(self):
        self.reports: list[dict] = []
        self.rejected: list[dict] = []

    def seen(self) -> frozenset:
        return frozenset(_person_key(p) for p in self.rejected)

    def compact(self) -> list[dict]:
        """The rejected, in a few tokens each: who, their title, and the answers."""
        return [{k: v for k, v in {
            "full_name": p.get("full_name"), "title": p.get("title"),
            "linkedin_url": p.get("linkedin_url"),
            "answers": {n: a.get("noul", a.get("choice", a.get("score")))
                        for n, a in ((p.get("qualification") or {}).get("answers") or {}).items()
                        if isinstance(a, dict)}}.items() if v}
            for p in self.rejected]

    def add(self, attempt: Optional[dict], report: dict) -> None:
        self.rejected += report.pop("_rejected", [])
        self.reports.append(report)
        if attempt is not None and report.get("status") == "done":
            attempt.update(qualified=report.get("qualified"),
                           not_qualified=report.get("not_qualified"))

    def receipt(self, qualify: dict) -> dict:
        done = [r for r in self.reports if r.get("status") == "done"]
        if not done:
            return self.reports[-1]     # skipped: says why (no key, no time…)
        out = {"tool": "jev_items", "status": "done", "model": done[-1].get("model"),
               "calls": len(done),
               "cost_usd": round(sum(r.get("cost_usd") or 0 for r in done), 8)}
        if "qualified" in done[-1]:
            out["not_qualified"] = len(self.rejected)
        return out


def _qualify_state(kind: str, rec: dict, inputs: dict) -> dict:
    state = {k: rec[k] for k in _QUALIFY_FIELDS[kind] if rec.get(k) not in (None, "", [])}
    if kind == "people":
        # The company is the lookup's, the same for every person: said once, from the inputs.
        state["company"] = inputs.get("company_name") or inputs.get("domain")
    return state


def _passes(answers: dict, questions: dict, min_p: Optional[float]) -> bool:
    """Every yes/no question at or above `min_p`; a question without an answer fails."""
    if min_p is None:
        return True
    for name, q in questions.items():
        if isinstance(q, dict) and q.get("type") == "noul":
            p = (answers.get(name) or {}).get("noul")
            if not isinstance(p, (int, float)) or p < min_p:
                return False
    return True


def _fit(p: dict) -> float:
    """How well a judged person fits: their LOWEST yes/no probability (0 unjudged) — the
    order the best `limit` are kept or revealed in."""
    answers = (p.get("qualification") or {}).get("answers") or {}
    ps = [a.get("noul") for a in answers.values()
          if isinstance(a, dict) and isinstance(a.get("noul"), (int, float))]
    return min(ps) if ps else 0.0


async def _qualify(mcp: FastMCP, sub, kind: str, found: dict, inputs: dict,
                   rubric: dict, deadline: float,
                   seen_rejected: frozenset = frozenset()) -> tuple[dict, dict]:
    """ONE `jev_items` call over what the cascade found: each record gets its answers
    (`qualification`), and with `min` the people below it are dropped. Jev's key is
    checked before the cascade (`_require_qualification`); what can still go wrong here
    — a refusal, no time left — never fails the lookup: the results come back
    unqualified, and the receipt says why."""
    records = found["people"] if kind == "people" else [found]
    report: dict = {"tool": "jev_items"}
    if kind == "people" and seen_rejected:
        # Already judged and rejected on an earlier step: never paid for twice.
        records = [r for r in records if _person_key(r) not in seen_rejected]
        found = {**found, "people": records, "count": len(records)}
        if not records:
            report.update(status="done", qualified=0, not_qualified=0, _rejected=[],
                          message="only people already rejected on an earlier step")
            return found, report
    if time.monotonic() > deadline - _QUALIFY_MIN_S:
        report.update(status="skipped", code="deadline",
                      message="no time left in this call to qualify the results")
        return found, report
    items = [{"key": str(i), "state": _qualify_state(kind, r, inputs)}
             for i, r in enumerate(records)]
    # `jev_items` takes 200 states a call: a big pool goes in chunks, judged together.
    chunks = [items[i:i + _JEV_CHUNK] for i in range(0, len(items), _JEV_CHUNK)]
    parts = [dict(report) for _ in chunks]
    issues = await asyncio.gather(*(
        appel._call(mcp, sub, "jev_items", {"items": c, "questions": rubric["questions"],
                                            "parallel": min(50, max(10, len(c)))}, part)
        for c, part in zip(chunks, parts)))
    by_index, model, cost = {}, None, 0.0
    for n, (issue, part) in enumerate(zip(issues, parts)):
        if issue is None or not appel._judge(issue, part):
            if n == 0:
                report.update(part)
                return found, report
            continue    # a later chunk refused: its people stay unjudged, kept
        res = _payload(issue) or {}
        model = model or res.get("model")
        cost += (res.get("usage") or {}).get("cost") or 0
        for a in res.get("answers") or []:
            if isinstance(a, dict) and a.get("index") is not None:
                by_index[a["index"] + n * _JEV_CHUNK] = a
    kept, rejected, failed = [], [], 0
    for i, r in enumerate(records):
        a = by_index.get(i) or {}
        if "answers" not in a:
            failed += 1     # no verdict is never a false one: kept, unqualified
            kept.append(r)
            continue
        ok = _passes(a["answers"], rubric["questions"], rubric["min"])
        r = {**r, "qualification": {"answers": a["answers"], "model": model,
                                    **({"passed": ok} if rubric["min"] is not None else {})}}
        if kind == "people" and not ok:
            rejected.append(r)
            continue
        kept.append(r)
    report.update(status="done", model=model, failed=failed, cost_usd=cost,
                  **({"calls": len(chunks)} if len(chunks) > 1 else {}))
    if kind == "people":
        report.update(qualified=len(kept), not_qualified=len(rejected), _rejected=rejected)
        return {**found, "people": kept, "count": len(kept)}, report
    return kept[0], report


#: The waterfalls whose finds a caller can have Jev qualify (`qualify=`).
_QUALIFIABLE = {"waterfall_company": "company", "waterfall_person": "person",
                "waterfall_people": "people"}


def _qualification_row(sub: Optional[str], ns: str) -> dict:
    """The optional Jev pass, for a screen and before a call: not a step (it finds
    nothing, it judges), so never in the order — whether it can run for `sub`. Jev runs
    on a SHARED key only (the tenant's, or the platform's): a closer one is refused.
    SYNC (database)."""
    mode = ordre._key_modes(sub, ["jev_items"]).get("jev_items", "unknown")
    return {"tool": "jev_items", "connector": "jev", "label": "Jev",
            "key_mode": mode,
            "usable": mode in ("tenant", "platform") or mode == "unknown",
            "shadowed": mode in ("org", "group", "user"),
            "fields": list(_QUALIFY_FIELDS[_QUALIFIABLE[ns]])}


def _require_qualification(sub: Optional[str], ns: str) -> None:
    """`qualify` asked and Jev unusable for `sub` (no shared key, or a closer key that
    shadows it): a NAMED refusal, raised before any step is paid. SYNC (database)."""
    row = _qualification_row(sub, ns)
    if row["usable"]:
        return
    why = ("a team, org or personal Jev key shadows the shared one, and Jev runs on the "
           "shared key only" if row["shadowed"] else "no Jev key is usable for you")
    raise _refused("qualification_unavailable",
                   f"`qualify` cannot run: {why} (key mode `{row['key_mode']}`). Nothing "
                   "was called — call again without `qualify`, or once Jev is available.")
