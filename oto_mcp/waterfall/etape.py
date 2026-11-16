"""What a step is: one provider's tool in a cascade (`Step`), the free collect call of
an asynchronous one (`Collect`), and what a step needs to run."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

@dataclass(frozen=True)
class Collect:
    """The free collect call of an asynchronous provider: `tool`, its arguments built
    from the SUBMIT result (None = the submit gave no handle), and whether a collected
    result is final."""
    tool: str
    args: Callable[[object], Optional[dict]]
    done: Callable[[object], bool]


@dataclass(frozen=True)
class Step:
    """One provider in a cascade.

    `requires`: alternative groups of normalized inputs — the step runs if ALL the
    inputs of at least ONE group are present (`(("linkedin_url",), ("email",))` = a
    LinkedIn URL OR an email). `build` turns the inputs into the tool's arguments;
    `extract(result, inputs)` reads the SERVED result (of the collect call, for an
    asynchronous step) and returns the find, or None."""
    key: str
    tool: str
    requires: tuple[tuple[str, ...], ...]
    build: Callable[[dict], dict]
    extract: Callable[[object, dict], object]
    cost: str
    # What a skipped step tells the caller it needed, when the input keys alone would
    # not say it (an internal flag is not something a caller can give).
    needs: str = ""
    collect: Optional[Collect] = None
    # An asynchronous step's OWN wait, `(first poll, then every, at most)` seconds, in
    # place of `appel.POLL_*` — inside the cascade's budget (`moteur.DEADLINE_S`), which
    # keeps the whole call under the REST bound: the steps after it get what is left.
    poll: Optional[tuple[float, float, float]] = None
    # Still not finished after its wait: the cascade goes on to the next steps (the
    # fallback) instead of stopping there. The step is listed `pending` all the same,
    # with its collect call, and read once more when the cascade ends (`moteur`).
    falls_through: bool = False
    # Costs the caller nothing (open data, their own session): ranks with their own keys.
    free: bool = False
    # Bills no credits but spends the KEY's call quota (lemlist's database search): never
    # counts against `max_paid_steps`, its pages are read like a free search's — yet it
    # keeps its written place in the order, unlike `free` (a shared quota is not nothing).
    quota_only: bool = False
    # What a screen calls the step, when the connector's own label would not tell two
    # steps apart (the French registry by SIREN, and by name).
    label: str = ""
    # What the step brings, in one line, for a screen and a person choosing their order:
    # what its source sees, and when it is worth its place. Facts the receipts taught,
    # never a price (that is the instance's own). Every row of every table has one
    # (`tests/waterfall/test_waterfall_plan.py`); the plan serves it as `explains`.
    explains: str = ""
    # A COMPLETION step: it does not compete for the find, it fills ONE field the find
    # still lacks (`linkedin_url`), after the cascade — see `moteur._run_cascade`.
    fills: str = ""
    # A completion that asks what the step keyed `same_as` asks (lemlist's company
    # database, for the LinkedIn page): skipped when that step already answered — found or
    # not, a second call would spend the quota for the same answer.
    same_as: str = ""
    # ONE row, several ways in (the French registry by SIREN, else by name): the variants
    # run in order, each when its own inputs are there, until one finds the value. The
    # receipt says which one answered (`via`). A screen shows one step.
    variants: tuple["Step", ...] = ()
    # A find too thin to stop on (Apollo's people search: every last name masked): kept
    # as the FALLBACK while the cascade looks further, returned only if nothing better
    # comes. The receipt marks the step `weak`.
    weak: Optional[Callable[[object], bool]] = None
    # The step runs on the caller's OWN key only (the shared key does not serve this
    # gesture: Apollo's phone reveal, a Forager email lookup): on any other key it is
    # skipped BEFORE the call, and a screen lists it with the ones to connect.
    own_key_only: bool = False
    # …unless the instance opens that gesture on its shared key after all (Forager's
    # `platform.ops` setting, `tools/forager.py::platform_ops`): SYNC (it reads the
    # instance's settings), asked once per call by `ordre._plan`, off the loop — True
    # lifts `own_key_only` for that call.
    shared_serves: Optional[Callable[[], bool]] = None
    # The tool that turns ONE of this step's (masked) finds into a usable one, billed per
    # person (Apollo: `apollo_match_person` by id). With `qualify=`, the cascade judges
    # the free finds first and reveals only those that pass (`vivier._reveal_qualified`).
    reveal: str = ""
    # A list that pages (`(parameter, first page)`). PAID: under `qualify_min`, its next
    # pages are bought one by one until `limit` people pass (`vivier._fill_from_pages`).
    # FREE (`quota_only`): under `qualify`, every page is read into the pool Jev judges
    # (`vivier._every_free_page`).
    pages: Optional[tuple[str, int]] = None
    # What each page read into the pool is reduced to BEFORE it is added (`(rows, inputs)`
    # → the rows `extract` still reads, in the same shape): a free search whose records
    # are heavy (lemlist: ~60 KB each, `experiences` cannot be excluded) would otherwise
    # hold up to `vivier.POOL_MAX` of them, whole, per row of a table.
    slim: Optional[Callable[[list, dict], list]] = None
    # Neutral facts a screen maps to its own price list (the trunk carries no price):
    # what the step asks its tool for when the tool bills by what is asked (`asks`:
    # FullEnrich's `work_emails` / `phones`, Forager's lookup), the most units one call
    # can bill (`max_units`: a search by name steps over namesakes), and whether it bills
    # per result returned, up to the call's `limit` (`per_item`).
    asks: str = ""
    max_units: Optional[int] = None
    per_item: bool = False
    # A SYNCHRONOUS call that would otherwise wait its client's own read bound (Pipecorn:
    # 60 s): the cascade passes it what is left of its budget as `timeout_s`, and skips
    # it, before any call, when too little is left (`moteur.BOUNDED_MIN_S`).
    bounded_by_deadline: bool = False
    # Its tool reads open data and resolves NO key (`fr_get`, `fr_search`), although its
    # connector carries one for other tools (`sirene`: the INSEE key of `fr_siret`): the
    # connector's key mode says nothing about this step — it runs as `open`, never
    # skipped `no_key` (the French registry was skipped in an org where it
    # answered when called directly).
    keyless: bool = False
    # ONE step, SEVERAL calls (Google: one search per role — it answers an empty list to
    # `"Acme" (a OR b OR c)`, measured, where `"Acme" "a"` finds): every argument set,
    # sent in turn — each billed by its tool, each counted against `max_paid_steps` —
    # until the find holds `limit` people, at most `max_calls`; their results made into
    # one by `merge`, read once by `extract`. `build` is the FIRST set.
    fan_out: Optional[Callable[[dict], list[dict]]] = None
    merge: Optional[Callable[[list], object]] = None
    max_calls: int = 1


def _bills(step: Step) -> bool:
    """A step whose call is paid, counted against `max_paid_steps` — not a free one, not
    a quota-only search, not a free search whose finds are revealed one by one (Apollo:
    the REVEALS are what is paid, counted one by one in `vivier._reveal_qualified`)."""
    return not (step.free or step.quota_only or step.reveal)


def _payload(issue):
    """What the step's tool RETURNED, out of its served `ToolResult` (structured content,
    else the JSON of its first block) — the same reading as the recipes
    (`redaction.extract_payload`). A tool that returns a list is served wrapped
    (`{"result": [...]}`): unwrapped here, so an extractor reads what the tool wrote."""
    from .. import redaction
    data = redaction.extract_payload(issue.result)
    if data is None and isinstance(issue.result, (dict, list)):
        data = issue.result
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


def _runnable(step: Step, inputs: dict) -> bool:
    if step.variants:
        return any(_runnable(v, inputs) for v in step.variants)
    return any(all(inputs.get(k) for k in group) for group in step.requires)


def _missing(step: Step) -> str:
    if step.needs:
        return step.needs
    if step.variants:
        return " or ".join(_missing(v) for v in step.variants)
    return " or ".join("+".join(g) for g in step.requires)
