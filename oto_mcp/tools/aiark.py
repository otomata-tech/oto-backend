"""AI Ark — B2B company/person search + contact enrichment (LinkedIn).

Classic connector (kind="tools", ex-mount #152→#160) on AI Ark's synchronous REST API
(docs.ai-ark.com). LLM contract curated here; the HTTP client lives in oto-core
(`oto.tools.aiark.client.AiArkClient`). Standard key cascade
(`resolve_api_key("aiark")`: BYO user/org > platform grant + quota) → platform
mode possible via `record_platform_usage`.

v1 = SYNCHRONOUS endpoints only. AI Ark's BULK exports/find-emails
answer by webhook (async) → out of scope (next iteration).
"""
from __future__ import annotations

import time
from typing import Literal, Optional

import requests

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection, session_org
from .lecture import LECTURE
from ..connectors import verify as connector_verify

# ── Triage view of a search page ─────────────────────────────────────────────────
# A `size=100` page returned 2.8 to 3.2 M characters: beyond a tool result's cap,
# hence a spill to file + a `jq` on EVERY page (11 pages in a single sourcing run).
# And an agent without a shell — bare MCP client, n8n — has no such escape hatch.
#
# DENYLIST of named keys, never an allowlist: an allowlist once made
# `liste_idcc` silently disappear on `fr_get` ("verified IDCC" field left empty on 500 rows).
# A denylist can only hide a field we wrote down; a new AI Ark field gets through.
#
# Measured on 14/08 on a real record (~12,000 chars): `position_groups` alone weighs
# more than half — the FULL description of every company the person
# went through, down to the 2010 internship. What sourcing reads fits in `profile` (name, title,
# headline), `location`, `link`, `department.seniority` and the company's identity.
_PERSON_DROP = ("educations", "volunteer_experiences", "awards", "skills", "languages",
                "member_badges", "statistics", "position_groups")
# `company` is KEPT (sourcing needs it) but lightened: these blocks are repeated
# identically on each of the 100 people of one company.
_COMPANY_DROP = ("technologies", "keywords", "naics", "industries", "languages",
                 "last_updated")

# ── Filters AI Ark ACCEPTS and NEVER applies ─────────────────────────────────────
# An unindexed filter key does not 400: the API swallows it and returns the WHOLE database,
# sorted by headcount. The agent then reads 72 M companies believing it is reading its
# filtered result — a false result, not a failure. Lived on 14/08 (`account.website` = finecobank.com →
# `totalElements` 72,343,404, Tata Group and Amazon topping "FinecoBank").
#
# Verified by DIFFERENTIAL on 15/08/2026 (same `size=1` query with and without the key):
#   account.website → 72,343,404 in BOTH forms (bare list AND SMART wrapper),
#                     where `account.domain` as a bare list returns 3.
#   contact.title   → 8,191,335 with and without, at the same first id.
# Hence the refusal rather than the warning: on a dead filter, everything that comes back is
# wrong, and nothing in the response says so.
_DEAD_FILTERS: dict[str, dict[str, str]] = {
    "account": {
        # Measured on 2026-09-02 (signal #642): `account.linkedin_url` in the wrapper
        # `{"any": {"include": […]}}` returns `totalElements` 72,508,445 — the whole
        # database, exactly like `website`. Same pathology, same refusal.
        "linkedin_url": 'domain, as a bare list: {"domain": {"any": {"include": '
                        '["example.com"]}}} — resolving a company by its LinkedIn '
                        "URL is not indexed at AI Ark",
        "website": 'domain, as a bare list: {"domain": {"any": {"include": '
                   '["example.com"]}}} (the wrapper {"mode": "SMART", "content": […]} '
                   "only applies to `name`)",
    },
    "contact": {
        "title": "seniority (+ location), then a CLIENT-SIDE sort of titles on the "
                 "returned pages — AI Ark does not index the job title",
        # Measured on 2026-09-03 (signal #694), two domains, strict differential:
        # publisher-a.example alone → 63; + `department: ["human_resources"]` → 63, the SAME
        # records in the SAME order (an author, a novelist, a publisher…),
        # none with `department.departments == ["human_resources"]`. Likewise
        # publisher-b.example + [human_resources, finance] → 68, of which zero HR and zero finance.
        # ⚠️ The `department.departments` field EXISTS on the returned records:
        # the data is indexed, it is the INPUT filter that does not bite. The trap
        # is therefore subtler than for `title` — you see the data, you think you can
        # filter on it. Cost: 63 records billed to keep zero or one.
        "department": "a CLIENT-SIDE sort on `department.departments` of the returned "
                      "pages (the field is present on every record) — "
                      "combined with `seniority` to reduce pagination",
    },
}


# ── … and those that are dead ONLY on the COMPANIES endpoint ─────────────────────
# Measured on 23/09/2026 by `size=1` differential (op="companies", `location` France +
# `employeeSize` 11-50), otomata-tech/oto#207:
#   control without `keywords`                               → 131,281
#   + `keywords: ["packaging"]`                              → 131,281
#   + `keywords: {"any": {"include": ["packaging"]}}`        → 131,281
#   + `keywords: {"any": {"include": {"mode": "SMART", …}}}` → 131,281
# All FOUR at the same first id. No form bites (same finding as the 12/09 report,
# 81,063 everywhere on another country). Refusal limited to `op="companies"`: on the
# PEOPLE search, `account.keywords` was not measured — refusing it there
# would be asserting what we do not know.
_DEAD_COMPANY_FILTERS: dict[str, dict[str, str]] = {
    "account": {
        "keywords": "`lookalike_domains` (up to 5 companies of the targeted sector), or "
                    "a CLIENT-SIDE sort on the `keywords` field of the records "
                    "returned with `full=True` (the default view removes it) — "
                    "narrowing first by `location` and `employeeSize`, which "
                    "do bite",
    },
}


def _reject_dead_filters(table: dict[str, dict[str, str]] = _DEAD_FILTERS,
                         **blocks) -> None:
    """Refuses a filter that AI Ark would accept without applying it (see `_DEAD_FILTERS`,
    and `_DEAD_COMPANY_FILTERS` for the companies endpoint)."""
    for block, value in blocks.items():
        for field, remedy in table.get(block, {}).items():
            if isinstance(value, dict) and field in value:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    f"Filter `{block}.{field}`: AI Ark accepts it and does NOT apply "
                    f"it — the search would return the whole database passing it off "
                    f"as a filtered result (verified by differential). "
                    f"Instead: {remedy}.")))
# Image URLs: an agent does not look at them.
#
# Widened on 10/09/2026 after measuring 11 real `op=people` records rendered
# by the triage view: **53% of the remaining weight was read by nobody**. `summary` (the
# LinkedIn "About") weighed 23% by itself — absent on most profiles,
# ~700 chars when present; the keys ALWAYS null on the sample (`middle_name`,
# `birth_date`, the three non-LinkedIn networks, `location.position`) 11%; the
# `department` sub-blocks 12%; `location.short/state` 7%. Same rule as the
# rest of the module: unread detail leaves the DEFAULT and comes back on `full=True`.
#
# ⚠️ `department.departments` is KEPT: it is the client-side sort that the
# refusal of the dead filter `contact.department` (`_DEAD_FILTERS`) recommends — removing it would make that
# remedy impossible to follow. `location.country` too: it is the only field that gives
# the country when `city` is empty (lived: "China, Asia", without a city).
_PROFILE_DROP = ("picture", "background", "summary", "middle_name", "birth_date")
_LINK_DROP = ("twitter", "github", "facebook")
_LOCATION_DROP = ("short", "state", "position")
_DEPARTMENT_DROP = ("sub_departments", "functions")


def _slim_company(company: object) -> object:
    if not isinstance(company, dict):
        return company
    out = {k: v for k, v in company.items() if k not in _COMPANY_DROP}
    loc = out.get("location")
    if isinstance(loc, dict) and "headquarter" in loc:
        # `locations[]` repeats the headquarters and its branches; the HQ is enough to locate.
        out["location"] = {"headquarter": loc["headquarter"]}
    return out


def _slim_person(row: object) -> object:
    if not isinstance(row, dict):
        return row
    out = {k: v for k, v in row.items() if k not in _PERSON_DROP}
    for bloc, drop in (("profile", _PROFILE_DROP), ("link", _LINK_DROP),
                       ("location", _LOCATION_DROP), ("department", _DEPARTMENT_DROP)):
        if isinstance(sous := out.get(bloc), dict):
            out[bloc] = {k: v for k, v in sous.items() if k not in drop}
    if "company" in out:
        out["company"] = _slim_company(out["company"])
    return out


def _shape(payload: object, op: str, full: bool, fields: Optional[list[str]]) -> object:
    """Tightened AI Ark page. `full=True` = the raw page, `fields=[…]` = only those keys.

    The name carries the intent (ADR 0047): `full=True` says what you get, where
    `compact=False` read as a double negative. And the default TIGHTENS: a
    saving you have to know about to benefit from benefits nobody — measured,
    no agent plugged in directly passed the opt-in.

    The envelope (`totalElements`, `totalPages`, `trackId`, pagination) is intact:
    without it the agent thinks it has seen everything."""
    if full or not isinstance(payload, dict):
        return payload
    rows = payload.get("content")
    if not isinstance(rows, list):
        return payload
    slim = _slim_person if op == "people" else _slim_company
    out = dict(payload)
    out["content"] = [slim(r) for r in rows]
    if fields:
        out = output_projection.project(out, items_path="content", fields=fields)
    return out


# ── TRANSPORT failure: AI Ark never answered ─────────────────────────────────────
# Signal #675 (2026-09-03, org 196): the EXPORT endpoint returns "Read timed out.
# (read timeout=30)" in bursts — 7 times on 4 profile URLs — while
# SEARCH answers normally in the same minutes. Replaying the IDENTICAL call eventually
# goes through.
#
# Two defects, measured on the real chain (tool → oto-core client → requests):
#   1. NO retry, neither here nor in the client: one attempt, then failure;
#   2. the failure came out WRAPPED in a `McpError(INVALID_PARAMS)` → the taxonomy
#      classed it `code="invalid_input"`, `retryable=false`. The SAME unwrapped timeout
#      is classed `upstream_timeout`, `retryable=true`, "try again in a moment":
#      our translation REVERSED the verdict the platform already knows how to give, and its
#      message ("could not process the request") blamed the input of a call AI
#      Ark never read.
#
# This is the worst place to get the verdict wrong. On an export, an agent told
# "your call is bad, do not retry" concludes an ABSENCE and writes
# `not_found` on someone nobody resolved.
#
# Hence: bounded retry, then the timeout PROPAGATES AS IS. Wrapping it would fix the
# message at the price of the verdict — `error_taxonomy.classify` handles any `McpError`
# first and never returns a `retryable` one, the two cannot be combined from
# here. What the message loses, the tool's description says (re-read on every call).
_TRANSPORT_TENTATIVES = 2      # the initial attempt + ONE retry
_TRANSPORT_PAUSE_S = 1.5


def _appel_avec_reprise(fn, client):
    """Runs `fn(client)`, retrying only TRANSPORT failures.

    `requests.exceptions.Timeout` = AI Ark never answered: neither a refusal nor an
    absence, NOTHING. It is the only case where replaying the identical call makes sense (measured:
    the next attempt goes through) and the only one where the agent must not conclude anything.
    Everything else — 4xx, 5xx, unreadable body — is a RESPONSE from AI Ark: we do not
    replay it, it goes to the error translator.

    Bounded to `_TRANSPORT_TENTATIVES`: one attempt costs up to 30 s of read timeout
    (`AiArkClient.TIMEOUT`, oto-core), so two still fit in a tool call's budget
    where three would tie up a pool thread for a minute and a half —
    and it is that pool that hung the box on 25/06. Beyond that, the retry goes back to the agent,
    and the response tells it so (`retryable: true`).
    """
    for n in range(1, _TRANSPORT_TENTATIVES + 1):
        try:
            return fn(client)
        except requests.exceptions.Timeout:
            if n == _TRANSPORT_TENTATIVES:
                raise
            time.sleep(_TRANSPORT_PAUSE_S)


def _verify(fields: dict, config: dict | None = None) -> dict:  # noqa: ARG001 (config: probe contract, unused here)
    """"Test the connection" probe — covers `auth+quota` (otomata-tech/oto#144).

    `verify_key()` (oto-core) does a credits GET with no side effect — 401 on an invalid
    key — and returns `{"valid": True, "credits": <int>}`. The balance used to be thrown away:
    on a zero account the key authenticates perfectly, the probe stayed green and
    a preflight went off to work only to hit 402s along the way. We therefore
    READ it, and a drained account raises `QuotaEpuise` (verdict `no_quota`).

    ⚠️ A positive balance does not guarantee a call will go through: AI Ark refuses PER
    ENDPOINT (measured on 07/09/2026: `op="people"` at 402 while
    `op="companies"` answered, same account, same instant). The probe proves "the
    key authenticates and the account is not at zero", nothing more.
    """
    from oto.tools.aiark.client import AiArkClient

    restant = AiArkClient(api_key=fields["key"]).verify_key().get("credits")
    # AI Ark now answers a DECIMAL balance (39519.7 seen on 07/10/2026): an int-only
    # check read a healthy account as "unreadable". A bool is not a balance.
    if not isinstance(restant, (int, float)) or isinstance(restant, bool):
        raise RuntimeError(
            f"AI Ark answered without a readable credit balance: {str(restant)[:200]}")
    if restant <= 0:
        raise connector_verify.QuotaEpuise(
            "The AI Ark key is good, but the account is drained (0 credits left). "
            "Top up the account at AI Ark — reconnecting would change nothing.")
    return {"quota": {"restant": restant, "unite": "credits"}}


def register(mcp: FastMCP) -> None:
    from oto.tools.aiark.client import AiArkClient

    connector_verify.register("aiark", _verify, couvre=connector_verify.AUTH_QUOTA)

    def _client() -> tuple[AiArkClient, bool]:
        key, is_platform = access.resolve_api_key("aiark")
        return AiArkClient(api_key=key), is_platform

    def _run(fn):
        """Runs an AI Ark call: translates an error RESPONSE into an actionable
        McpError and counts platform usage on success.

        ⚠️ An upstream 5xx does NOT clear the input — the message therefore no longer
        claims it (it used to, word for word like the Kaspr connector, where it was
        false: Kaspr returns 500 on an unknown `dataToGet`). The retry is bounded
        to one deferred attempt, consistent with the `retryable: false` the
        taxonomy returns for this McpError: "replayable as is" is not the
        first move when the input may be at fault.

        A TRANSPORT failure, by contrast, says nothing about the input: AI Ark did not read
        the call. It is retried once (`_appel_avec_reprise`) then propagates BARE —
        see the `_TRANSPORT_*` block. The distinction is the whole fix for
        signal #675: here, a badly translated error reads as an absence."""
        client, is_platform = _client()
        try:
            result = _appel_avec_reprise(fn, client)
        except McpError:
            raise
        except requests.exceptions.Timeout:
            # DO NOT wrap: as is, the taxonomy returns `upstream_timeout` /
            # `retryable: true`; wrapped, it would return `invalid_input` /
            # `retryable: false` — "fix your call" on a call never read.
            raise
        except Exception as e:
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status and status >= 500:
                msg = (f"AI Ark returned a server error ({status}). An upstream 5xx "
                       "does not prove an outage: first check the call's "
                       "parameters. If the input is correct: a single further "
                       "attempt, deferred.")
            elif status == 401:
                msg = "AI Ark key invalid or revoked (401). Check the key that is set."
            elif status == 402:
                # 402 = the ACCOUNT refuses, for lack of credits (otomata-tech/oto#144).
                # Rendered by the generic branch, it said "could not process the
                # request": the agent fixed its input, replayed a request
                # that had already succeeded at `size=3`, then stopped without knowing why
                # (run stopped at 120 rows out of 438, 17/08/2026). The refusal is PER
                # ENDPOINT: `op="companies"` can answer while
                # `op="people"` returns 402 — it is not a hint about the input.
                recharge = ("These credits are provided by oto: report it "
                            "(`feedback`, signal='gap'), or set your own AI Ark key."
                            if is_platform else
                            "Top up the account at AI Ark, then resume where you "
                            "stopped.")
                msg = ("AI Ark refused the call for lack of credits (402) — it is the "
                       "account that is drained on this endpoint, not your input. "
                       "Neither reducing `size`, nor changing page or filters, nor "
                       f"retrying will change anything until it is topped up. "
                       f"{recharge}")
            else:
                msg = f"AI Ark could not process the request ({e})."
            raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))
        if is_platform:
            access.record_platform_usage("aiark")
        return result

    @mcp.tool(annotations=LECTURE)
    def linkedin_aiark_credits() -> dict:
        """Remaining AI Ark credits for the resolved account (`{"total": <int>}`).

        ⚠️ On the PLATFORM key (credits paid by oto), the balance is not yours to
        read — the call is refused rather than showing someone else's pool. Bring
        your own AI Ark key to see a balance.
        """
        _, is_platform = _client()
        if is_platform:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                "These AI Ark credits are provided by oto: their balance is not "
                "exposed to you. Set your own AI Ark key to track a balance.")))
        return _run(lambda c: c.credits())

    @mcp.tool(annotations=LECTURE)
    def linkedin_aiark_search(
        op: Literal["people", "companies"] = "people",
        account: Optional[dict] = None,
        contact: Optional[dict] = None,
        lookalike_domains: Optional[list[str]] = None,
        lists: Optional[dict] = None,
        page: int = 0,
        size: int = 10,
        full: bool = False,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Search LinkedIn-sourced B2B data through AI Ark (bought data, per-credit).

        Not interchangeable with `linkedin_unipile_search`: that one drives YOUR
        connected LinkedIn session (and is rate-limited by LinkedIn); this one
        queries AI Ark's index and BILLS CREDITS per returned record.

        `op`:
        - **"people"** (default): people by company + contact filters. Results do
          NOT include emails — use `linkedin_aiark_person(op="export")` for one.
        - **"companies"**: companies by firmographics.

        Returns the AI Ark page: `content[]`, `totalElements`, `totalPages`.

        A read timeout is retried ONCE and then raised with `retryable: true` — a
        failure, never an empty page (⚠️ the retry can bill a second time if AI Ark
        had already processed the first attempt).

        Args:
            op: "people" (default) | "companies".
            account: filters on the company. AI Ark nested DSL — each field takes an
                include/exclude matcher. Examples:
                - name: {"name": {"any": {"include": {"mode": "SMART", "content": ["Amazon"]}}}}
                - location: {"location": {"any": {"include": ["United States"]}}}
                  ⚠️ This is the **HEADQUARTERS**, not "has an office there". A company whose
                  HQ is elsewhere returns `0` even if it employs hundreds of
                  people in the requested country, and the failure is indistinguishable from
                  "nobody matches". Measured on 04/09/2026: `account.location
                  = United Kingdom` on a bank headquartered in New York → 0;
                  `contact.location = United Kingdom` on the same → 89 people.
                  For "who works in this country", filter on `contact.location`.
                - employee size: {"employeeSize": {"type": "RANGE", "range": [{"start": 1000, "end": 5000}]}}
                - a company's site: {"domain": {"any": {"include": ["example.com"]}}}
                  — plain list, NOT the SMART wrapper (that one is for `name` only).
                Combine keys in one object. Keys MEASURED to bite on
                op="companies": `domain`, `employeeSize`, `location`.
                `industries`, `technologies`, `naics` answered 400 under the forms
                tried (a loud failure, but their expected shape is unknown).
                ⚠️ REFUSED because AI Ark accepts them and silently ignores them,
                returning the whole database as if it were your filtered result:
                `website` and `linkedin_url` (filter on `domain` instead) and, on
                op="companies", `keywords` — dead under every form tried (plain
                list, `any.include`, SMART wrapper). To target a sector, use
                `lookalike_domains`, or sort client-side on the records' `keywords`
                (`full=True`).
            contact: op="people" — filters on the person, e.g.
                {"seniority": {"any": {"include": ["founder"]}}}. Supports seniority
                and location.
                ⚠️ `seniority` is a **normalized level, derived from the job
                title** — not the title itself. In sectors where the title does not
                follow the hierarchy (investment banking, consulting), it massively
                discards the right people: measured on 04/09/2026, adding
                `seniority: "director"` to a query that returned 89 people brought it
                down to 2, the first of whom was a human resources
                director. A "Managing Director" does not carry `director`. Use it
                to reduce pagination, never as a selection criterion.
                ⚠️ `title` and `department` are REFUSED for the same reason: AI Ark
                accepts them and silently ignores them, so you get the company's first
                page and pay for it. Filter on `seniority`, then sort client-side —
                `department.departments` IS on every record returned, it just cannot
                be filtered on.
            lookalike_domains: op="companies" — up to 5 company URLs to find similar ones.
            lists: exclude records already in saved lists.
            page: zero-based page number. size: 0-100 (default 10).
            full: True = the RAW AI Ark record, every block. The DEFAULT is a sourcing
                view that drops what sourcing never reads — past positions with the
                full write-up of every company worked at, education, volunteering,
                awards, skills, badges, statistics, languages, image URLs, the
                profile `summary`, birth date, non-LinkedIn social links and the
                `department` sub-blocks — plus the company blocks repeated
                identically on all 100 people of one firm. A `size=100` page
                returned ~3 M characters, past any tool-result cap; measured
                10/09/2026, 53 % of what the sourcing view still carried was read
                by nobody.
            fields: keep ONLY these keys on each record; the envelope (totals,
                pagination, trackId) always stays — without it you would think you
                saw everything. Combine with `full=True` to project the raw record.
                ⚠️ These are the REAL top-level keys. op="people", default view:
                `id`, `identifier`, `profile`, `link`, `location`,
                `industry`, `department`, `company`, `last_updated` (with
                `full=True`, the blocks the view removes are added).
                op="companies", the same block as a person's `company`, default
                view: `id`, `summary`, `link`, `location` (+ `industries`,
                `technologies`, `keywords`, `naics`, `languages`, `last_updated`
                with `full=True`). An unrecognized
                name is **silently discarded**, not refused: a projection of
                invented names returns records that look EMPTY, and
                the absence reads as "no data" instead of "wrong key"
                (signal 717). Omitting `company` is in fact the whole point of the
                projection — it is the block repeated identically on every person
                of the same company.
        """
        _reject_dead_filters(account=account, contact=contact)
        if op == "people":
            result = _run(lambda c: c.search_people(
                account=account, contact=contact, lists=lists, page=page, size=size))
        elif op == "companies":
            _reject_dead_filters(_DEAD_COMPANY_FILTERS, account=account)
            result = _run(lambda c: c.search_companies(
                account=account, lists=lists,
                lookalike_domains=lookalike_domains, page=page, size=size))
        else:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message="op must be 'people' or 'companies'"))
        # Per-unit metering (partner billing, 21/08): the number of records RETURNED
        # in this page, not the requested `size` (a page at the end of the results can
        # return fewer). It is what AI Ark actually bills ("BILLS CREDITS per
        # returned record", docstring above) — the same axis this tool must
        # expose to `tool_calls.quantity`.
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            session_org.note_call_trace(quantity=len(content))
        return _shape(result, op, full, fields)

    @mcp.tool(annotations=LECTURE)
    def linkedin_aiark_person(
        op: Literal["export", "reverse", "mobile"] = "export",
        id: Optional[str] = None,
        url: Optional[str] = None,
        search: Optional[str] = None,
        linkedin: Optional[str] = None,
        domain: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict:
        """Resolve ONE person through AI Ark (bought data, per-credit).

        `op`:
        - **"export"** (default): the person WITH their email (synchronous email
          finder). Give `id` (from `linkedin_aiark_search(op="people")`) OR `url`
          (a LinkedIn profile URL).
        - **"reverse"**: find the person FROM a contact detail (`search` = an email,
          a phone number…).
        - **"mobile"**: their mobile phone number(s). Give `linkedin` (profile URL)
          alone, OR `domain` AND `name` together.

        Every op returns `{"found": false}` rather than an error when nothing
        matches — an absence is a result, not a failure.

        The converse matters more, and is the whole point of this note: a FAILURE is
        never an absence. When AI Ark does not answer at all (read timeout — it comes
        in bursts on `op="export"` while search stays healthy in the same minutes),
        the call is retried ONCE, then raised as an error carrying `retryable: true`.
        Nobody was looked up: retry it, and never record a not-found from it — the
        person may well have an e-mail nobody has asked for yet.
        ⚠️ That retry can bill a second credit if AI Ark had already processed the
        first attempt and only the answer was lost.

        Args:
            op: export (default) | reverse | mobile.
            id: op="export" — an AI Ark person id from a prior search.
            url: op="export" — a LinkedIn profile URL.
            search: op="reverse" — the contact detail to resolve.
            linkedin: op="mobile" — the person's LinkedIn profile URL.
            domain: op="mobile" — the company domain (with `name`).
            name: op="mobile" — the person's name (with `domain`).
        """
        def _need(cond: bool, msg: str) -> None:
            if not cond:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))

        if op == "export":
            _need(bool(id or url), "op='export' requires `id` or `url`.")
            result = _run(lambda c: c.export_person(id=id, url=url))
        elif op == "reverse":
            _need(bool(search), "op='reverse' requires `search`.")
            result = _run(lambda c: c.reverse_lookup(search))
        elif op == "mobile":
            _need(bool(linkedin) or bool(domain and name),
                  "op='mobile' requires `linkedin` OR (`domain` AND `name`).")
            result = _run(lambda c: c.mobile_phone(
                linkedin=linkedin, domain=domain, name=name))
        else:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="op must be 'export', 'reverse' or 'mobile'"))

        if result is None:
            return {"found": False}
        return {"found": True, **result}
