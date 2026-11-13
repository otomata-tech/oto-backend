"""Pipecorn tool — waterfall contact enrichment (mobile phone, work email), asynchronous;
a person's profile, a company's firmographics, technologies and job postings,
synchronous; and the account's balance.

Wraps `oto.tools.pipecorn.PipecornClient`. The key comes from the cascade (own key >
platform grant) via `access.resolve_credential("pipecorn")`.

ONE tool, `pipecorn` (LECTURE): a lookup changes nothing at Pipecorn, it spends
credits — like any enrichment read here. No `pipecorn_act` (`tests/test_convention_act.py`).

**Asynchronous.** `op="enrich"` SUBMITS the lookup (Pipecorn queues it and answers at
once with an `enrichment_id`); `op="result"` reads it back (free), `pending` until
Pipecorn's own cascade of providers is done. `waterfall_phone` and `waterfall_email` poll
it with their own wait (`waterfall/fournisseurs.py::PIPECORN_POLL`). The profile, company,
technologies and job postings (`person`, `company`, `stack`, `jobs`) are synchronous;
`timeout_s` bounds their wait (a cascade passes what is left of its budget,
`Step.bounded_by_deadline`), and no answer within it is the named refusal
`pipecorn_timeout`, never a success: Pipecorn may have billed the lookup.

**A third party's personal email is refused**, on every key, before anything is resolved
or sent (`pipecorn_personal_email_refused`): the client refuses it too. The synchronous
lookups are priced for no contact: their answers are served through the client's
`without_contacts` (every email and phone field removed, a hiring team's included).

**Metering** (facts, no price), UNCONDITIONAL — own key or shared key: the consumer
filters on `key_mode`. Pipecorn bills per result FOUND, in its credits, when the lookup
finishes: 3 a work email, 30 a phone — a phone only when a MOBILE is found
(`mobile` or `fixed_or_mobile`); a miss costs nothing. Nothing in the answer says what
it cost: the client reads it from what was found (`found_kinds`, `credits_spent`,
bounded by what was asked — `find`, echoed by the submission and passed back to the
result).
- the submission traces `quantity=0` (nothing is spent yet), and on the shared key
  `units=1` (one lookup against the grant's quota);
- a read still `pending`, or `failed`, traces `quantity=0` (and `units=0` on the shared
  key);
- a FINISHED read traces `quantity` = 1 when something asked was found, else 0 (never
  absent: the aggregated view reads an absence as 1), `provider_credits` = the
  Pipecorn credits spent, and the three `found_work_emails` / `found_personal_emails`
  (always 0) / `found_phones` together (the billing lens reads them all or nothing).
  Every finished read of the same lookup traces the same figures: the consumer counts
  it once by its `enrichment_id` (the read's argument, the lens's `job_id`,
  `db.usage.BILLABLE_JOB_ARGS`).
- a synchronous lookup traces, on its call, `quantity` = 1 when it found something (a
  person, a company, a technology, a job), else 0 (never absent), and
  `provider_credits` = the Pipecorn credits it cost (`LOOKUP_CREDITS`): 1 a person or a
  company resolved, 3 a technology stack (per request, found or not; a 404 aside), 1 per
  job returned.

**Units budget** (`access/credits.py`): the submission is in the perimeter, as the
connectors library's `pipecorn.submit_enrichment`, PRICED PER KIND FOUND — the units
`work_emails` and `phones`, each under its own scale line, like FullEnrich. On a
re-billed key (the platform's, or the primary tenant's) its worst case, one find per
kind asked, is reserved before the lookup is sent; the read that finds it FINISHED
settles each kind at what was found (`credits_releve.regler_releve`), a `failed` one
releases it, and one never read is settled at the amount reserved when its hold expires
(`credits_echeances`). On the customer's own key nothing is reserved: Pipecorn bills
that key.

The synchronous lookups are NOT in the perimeter: it holds one connector function per
tool (`credits.declarer`, keyed by the tool's name — this one is
`pipecorn.submit_enrichment`), and a synchronous settlement reads the quantity in the
answer (`cost.settle`) or settles the worst case, where no Pipecorn answer says what it
cost (a miss would settle 1 credit, a job list its 100). Hence:

**The shared key** (any key that is not the caller's own: the platform's, a tenant's)
serves the contact lookup — a phone, a work email —, in the units budget above. The four
synchronous lookups, outside it, and the balance (the key owner's) ask for the
customer's own key, refused BEFORE anything is sent. A result is read with the key that
submitted it.

Client calls are written out (`client.submit_enrichment(…)`) so the version-skew
probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection, session_org
from ..access import credits as budget
from ..access import credits_releve
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from ..waterfall.ordre import _OWN_KEY
from .lecture import LECTURE

_NAME = "pipecorn"

#: What a contact lookup may ask for — all of it served on the shared key, in the units
#: budget (`_pire_par_nature`).
FINDS = ("phone", "email")

#: The synchronous lookups, and the client method each calls.
LOOKUPS = {"person": "enrich_person", "company": "enrich_account",
           "stack": "company_stack", "jobs": "company_hiring"}

#: What a lookup's answer names when it found someone or something.
_NAMED_BY = {"person": ("full_name", "title", "positions"),
             "company": ("company_name", "domain", "linkedin_url")}

#: A 404 on a synchronous lookup: no such person or company, nothing billed.
_MISS: dict = {}

#: The kinds a lookup asks, as the units budget's units (FullEnrich's names): what is
#: priced per lookup FOUND, kind by kind.
_NATURES = {"email": "work_emails", "phone": "phones"}

#: Why a lookup ended `failed`: credits ran out while it ran, nothing was found.
_FAILED = {
    "insufficient_credits": "the Pipecorn balance ran out while it ran",
    "workspace_limit_exceeded": "the key's weekly credit limit was reached while it ran",
}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refus(code: str, message: str, **data) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=f"Refusal `{code}`: {message}",
                              data={"code": code, "retryable": False, **data}))


def _finds(find) -> list[str]:
    return list(dict.fromkeys(find or ["phone"]))


def _pire_par_nature(args: dict) -> dict:
    """The worst case of a submission, per kind asked: one find of each."""
    from oto.connectors.cost import CostError
    finds = _finds(args.get("find"))
    if (args.get("op") or "enrich") != "enrich" or any(f not in _NATURES for f in finds):
        raise CostError(f"pipecorn: no submission, or a kind without a unit in {finds!r}")
    return {_NATURES[f]: 1 for f in finds}


def _releve(enrichment_id: str) -> tuple[str, dict]:
    """The collection of a submitted lookup, as `cost.collection_of` names it."""
    return "pipecorn.get_enrichment", {"enrichment_id": enrichment_id}


def _code(e) -> str:
    return e.body.get("code", "") if isinstance(e.body, dict) else ""


def _error(e) -> str:
    if isinstance(e.body, dict):
        return str(e.body.get("error") or e.body)
    return str(e.body or "")


def _upstream_message(e, is_platform: bool = False) -> str:
    """Turn a Pipecorn refusal into an actionable message, by status and `code`."""
    status = e.status_code
    if status == 402 or _code(e) == "insufficient_credits":
        if is_platform:
            return ("Pipecorn: the shared key's credit balance is empty (402) — nothing "
                    "was spent. Tell the instance's administrator; do not retry.")
        return ("Pipecorn: not enough credits left on your account for this lookup (402) "
                "— nothing was spent. Top up in Pipecorn, then retry.")
    if status == 403 and _code(e) == "workspace_limit_exceeded":
        return ("Pipecorn: this key's weekly credit limit for its workspace is reached "
                "(403) — a workspace admin raises it in Pipecorn, or wait for the week "
                "to reset.")
    if status == 401:
        return ("Pipecorn rejected the API key (401): copy it again from Settings → API "
                "keys; a rotated key stops working at once.")
    if status == 403:
        return f"Pipecorn refused this call (403): {_error(e)}"
    if status == 404:
        return ("Pipecorn has no such lookup for this key (404): check the "
                "`enrichment_id` — a lookup is read with the key that submitted it.")
    if status in (400, 422):
        return f"Pipecorn rejected the request ({status}): {_error(e)}"
    if status == 429:
        return "Pipecorn: too many requests (429) — retry in a few seconds."
    if status in (500, 502, 503, 504):
        return (f"Pipecorn failed or timed out (HTTP {status}). A read or the balance "
                "can be retried; a submitted lookup may have been accepted: do not submit "
                "it again blindly.")
    return f"Pipecorn refused the request (HTTP {status}): {_error(e)}"


def _verify(fields: dict, config: dict | None = None) -> dict:  # noqa: ARG001
    """"Test the connection" probe: the balance (`GET /credits`), free — it also says
    whether enrichment credits are left. An unreadable answer or balance is an outage,
    never a refused key nor a healthy account. Returns that balance (`{"quota": …}`):
    it used to be read then thrown away, so the platform key showed `balance: null`."""
    from oto.tools.common.credentials import MissingCredential
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.pipecorn import PipecornClient, PipecornUnreadable

    try:
        balance = PipecornClient(api_key=fields.get("key")).probe()
    except MissingCredential as e:
        raise connector_verify.NonAutorise(str(e))
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Pipecorn HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Pipecorn HTTP {e.status_code}: {e.body}")
    except PipecornUnreadable as e:
        raise RuntimeError(f"Pipecorn: unreadable answer to the balance read — {e}")
    left = connector_verify.nombre_lu(
        ((balance if isinstance(balance, dict) else {}).get("credits") or {}).get("enrichment"))
    if left is None:
        raise RuntimeError("Pipecorn answered without a readable enrichment balance: "
                           f"{str(balance)[:200]}")
    if left <= 0:
        raise connector_verify.QuotaEpuise("Pipecorn: no enrichment credits left.")
    return {"quota": {"restant": left, "unite": "credits"}}


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.pipecorn import (PipecornClient, PipecornUnreadable, credits_spent,
                                    found_kinds, without_contacts)
    from oto.tools.pipecorn.client import HTTP_TIMEOUT, LOOKUP_CREDITS
    from requests.exceptions import Timeout

    connector_verify.register(_NAME, _verify, couvre=connector_verify.AUTH_QUOTA)
    budget.declarer(_NAME, _NAME, "submit_enrichment", unites=_pire_par_nature)

    def _client(op: str) -> tuple[PipecornClient, "access.ResolvedCredential"]:
        """`(client, rc)`. Only a submission resolves with the usage checks (and, on a
        re-billed key, reserves its worst case): a read, a synchronous lookup on the
        caller's own key, the balance and the account resolve without them. Refused
        BEFORE anything is sent, naming what the shared key serves: a synchronous lookup
        on any key that is not the caller's own (outside the units budget), the balance
        and the account on the platform key."""
        rc = access.resolve_credential(_NAME, check_usage=(op == "enrich"))
        if (op in LOOKUPS and rc.mode not in _OWN_KEY) or \
                (op in ("credits", "account") and rc.is_platform):
            from ..auth.hooks import current_user_sub_from_token
            from .. import links
            sub = current_user_sub_from_token()
            why = ("the balance is the key owner's" if op in ("credits", "account") else
                   "a synchronous lookup is not in this instance's credit budget yet, so "
                   "the shared key does not serve it")
            raise _bad(
                f"pipecorn op={op!r} needs your own Pipecorn key: {why}. The shared key "
                "here covers phone and work email lookups. Set your own key"
                f"{links.ou_poser_la_cle(sub, org=access.current_org(sub), connecteur=_NAME)}.")
        return PipecornClient(api_key=rc.key), rc

    def _run(fn, is_platform: bool = False, miss=None):
        """The call; `miss` (a synchronous lookup) is what a 404 answers — nothing found,
        nothing billed — instead of a refusal."""
        try:
            return fn()
        except ValueError as e:
            # The client's own checks, before anything is sent: nothing consumed.
            budget.rien_envoye()
            raise _bad(str(e))
        except PipecornUnreadable as e:
            raise _refus("pipecorn_unreadable_answer",
                         f"Pipecorn answered something that is not JSON — nothing could "
                         f"be read from it ({e}). Retry later; a submitted lookup may "
                         "have been accepted: read it back rather than submit it again.")
        except UpstreamHTTPError as e:
            if miss is not None and e.status_code == 404:
                return miss
            raise _bad(_upstream_message(e, is_platform))

    def _lookup(op: str, client, given: dict, timeout_s) -> dict:
        """A synchronous lookup, on the caller's own key: served without its contact
        fields, traced `quantity` 1 when it found something (else 0, never absent) and
        `provider_credits` what Pipecorn billed (`LOOKUP_CREDITS`). Waits `timeout_s`
        at most (the client's own read bound otherwise): no answer within it is the
        refusal `pipecorn_timeout`."""
        company = {"domain": given["domain"], "name": given["company_name"],
                   "linkedin_url": given["company_linkedin_url"]}
        wait = HTTP_TIMEOUT if timeout_s is None else (min(10, timeout_s), timeout_s)
        calls = {
            "person": lambda: client.enrich_person(
                linkedin_url=given["linkedin_url"], first_name=given["firstname"],
                last_name=given["lastname"], company_name=given["company_name"],
                timeout=wait),
            "company": lambda: client.enrich_account(
                **company, country_code=given["country_code"], timeout=wait),
            "stack": lambda: client.company_stack(**company, timeout=wait),
            "jobs": lambda: client.company_hiring(
                **company, posted_within_days=given["posted_within_days"],
                limit=given["limit"], job_title_keywords=given["job_title_keywords"],
                excluded_job_title_keywords=given["excluded_job_title_keywords"],
                job_country_codes=given["job_country_codes"],
                full_description=given["full_description"], timeout=wait),
        }
        try:
            raw = _run(calls[op], miss=_MISS)
        except Timeout:
            # Sent, and no answer in time (connect or read): never a miss nor a find.
            # What Pipecorn did with it is unknown — nothing is traced as found.
            raise _refus("pipecorn_timeout",
                         f"Pipecorn op={op!r}: no answer within {wait[1]:g} s; Pipecorn "
                         "may have billed the lookup. Do not retry it blindly.",
                         timeout_s=wait[1])
        if not isinstance(raw, dict):
            raise _refus("pipecorn_unreadable_answer",
                         f"Pipecorn answered op={op!r} with something that is not an "
                         f"object: {str(raw)[:200]}")
        answer = without_contacts(raw)
        price = LOOKUP_CREDITS[LOOKUPS[op]]
        if op == "jobs":
            jobs = answer.get("jobs") if isinstance(answer.get("jobs"), list) else []
            found, credits = bool(jobs), price * len(jobs)
        elif op == "stack":
            # Billed per REQUEST, found or not — a 404 (no such company) aside.
            found = bool(answer.get("technologies") or
                         answer.get("technologies_with_confidence"))
            credits = 0 if raw is _MISS else price
        else:
            found = any(answer.get(k) for k in _NAMED_BY[op])
            credits = price if found else 0
        session_org.note_call_trace(quantity=int(found), provider_credits=credits)
        return {**answer, "found": found, "pipecorn_credits": credits}

    @mcp.tool(name="pipecorn", annotations=LECTURE)
    def pipecorn(
        op: Literal["enrich", "result", "person", "company", "stack", "jobs", "credits",
                    "account"] = "enrich",
        find: Optional[list[Literal["phone", "email", "personal_email"]]] = None,
        linkedin_url: Optional[str] = None,
        firstname: Optional[str] = None,
        lastname: Optional[str] = None,
        domain: Optional[str] = None,
        company_name: Optional[str] = None,
        company_linkedin_url: Optional[str] = None,
        country_code: Optional[str] = None,
        phone_type: Optional[Literal["all", "mobile"]] = None,
        phone_country_codes: Optional[list[str]] = None,
        enrichment_id: Optional[str] = None,
        posted_within_days: Optional[Literal[7, 30, 60, 90, 180]] = None,
        limit: Optional[int] = None,
        job_title_keywords: Optional[list[str]] = None,
        excluded_job_title_keywords: Optional[list[str]] = None,
        job_country_codes: Optional[list[str]] = None,
        full_description: Optional[bool] = None,
        timeout_s: Optional[float] = None,
    ) -> dict:
        """Pipecorn — a person's mobile phone or work email (asynchronous: submit, then
        read the result); a person's profile; a company's firmographics, technologies
        and open job postings; the account's balance.

        You only pay for what is found, in Pipecorn credits: 3 a work email, 30 a phone
        (only when a MOBILE is found), 1 a profile or a company resolved, 1 per job
        posting returned; a technology stack is 3 per request. A miss costs nothing. A
        third party's personal email is never looked up nor served: `personal_email` is
        refused on any key, and the profile, company and job lookups come without any
        email or phone.

        op="enrich" (default) — SUBMIT a contact lookup; answers at once with
          `enrichment_id`. `find`: `phone`, `email` or both (default `["phone"]`).
          `phone` needs `linkedin_url` (a profile URL); `email` takes `linkedin_url`,
          or `firstname` + `lastname` + `domain` or `company_name`.
          `phone_type="mobile"` keeps looking until a provider has a mobile;
          `phone_country_codes=["FR"]` keeps phones to countries.
        op="result" — read it back: `enrichment_id`, and the same `find`. `done: false`
          while Pipecorn is still looking — read again in a few seconds (reads are
          free). Done: `email`, `email_status`, `phone` (as Pipecorn writes them),
          `phone_details` [{number, type}], and `pipecorn_credits`, what it cost.
        op="person" — a profile: title, positions with each company's domain,
          headline, location, tenure. `linkedin_url`, or `firstname` + `lastname` +
          `company_name`.
        op="company" — firmographics: industry, headcount and its growth, department
          headcount, revenue range, headquarters, funding, web traffic. `domain`,
          `company_name` and/or `company_linkedin_url`; `country_code` narrows a name.
        op="stack" — the technologies a company uses, with a confidence. Same company
          arguments.
        op="jobs" — a company's open job postings (title, url, location, date, hiring
          team). Same company arguments; `posted_within_days` (7, 30 by default, 60,
          90, 180), `limit` (at most 100: billed per job, so bound it),
          `job_title_keywords`, `excluded_job_title_keywords`, `job_country_codes`;
          `full_description=True` adds each description in full.
          person, company, stack and jobs answer `found` and `pipecorn_credits`, and
          need your own Pipecorn key. `timeout_s` bounds their wait in seconds; no
          answer within it is the refusal `pipecorn_timeout` (it may have been billed).
        op="credits" — the balance: `credits.enrichment` is what lookups spend.
        op="account" — the plan and the email behind the key.

        For a contact, prefer the waterfalls (`waterfall_phone`, `waterfall_email`):
        they submit and read this for you.

        Examples —
          pipecorn(find=["phone"], linkedin_url="https://www.linkedin.com/in/jane-doe/",
                   phone_type="mobile")
          pipecorn(op="result", enrichment_id="…", find=["phone"])
          pipecorn(op="jobs", domain="acme.com", limit=20, job_country_codes=["FR"])
        """
        given = {"find": find, "linkedin_url": linkedin_url, "firstname": firstname,
                 "lastname": lastname, "domain": domain, "company_name": company_name,
                 "company_linkedin_url": company_linkedin_url, "country_code": country_code,
                 "phone_type": phone_type, "phone_country_codes": phone_country_codes,
                 "enrichment_id": enrichment_id, "posted_within_days": posted_within_days,
                 "limit": limit, "job_title_keywords": job_title_keywords,
                 "excluded_job_title_keywords": excluded_job_title_keywords,
                 "job_country_codes": job_country_codes,
                 "full_description": full_description, "timeout_s": timeout_s}
        company = {"domain", "company_name", "company_linkedin_url", "timeout_s"}
        takes = {"enrich": {"find", "linkedin_url", "firstname", "lastname", "domain",
                            "company_name", "phone_type", "phone_country_codes"},
                 "result": {"enrichment_id", "find"},
                 "person": {"linkedin_url", "firstname", "lastname", "company_name",
                            "timeout_s"},
                 "company": company | {"country_code"},
                 "stack": company,
                 "jobs": company | {"posted_within_days", "limit", "job_title_keywords",
                                    "excluded_job_title_keywords", "job_country_codes",
                                    "full_description"}}.get(op, set())
        stray = sorted(k for k, v in given.items() if v is not None and k not in takes)
        if stray:
            raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                       "Nothing was done.")
        if timeout_s is not None and (isinstance(timeout_s, bool) or timeout_s <= 0):
            raise _bad(f"`timeout_s` must be a positive number of seconds; got "
                       f"{timeout_s!r}. Nothing was done.")
        finds = _finds(find)
        if "personal_email" in finds:
            raise _refus("pipecorn_personal_email_refused",
                         "a third party's personal email is never looked up, on any "
                         "key. Ask for `email` (work email) or `phone`. Nothing was done.")
        client, rc = _client(op)
        if op in LOOKUPS:
            answer = _lookup(op, client, given, timeout_s)
            if op == "jobs" and not full_description:
                # The default view: each posting without its description snippet —
                # title, place, date, link and hiring team are what a list is read for.
                answer = output_projection.project(answer, items_path="jobs",
                                                   item_drop=("description",))
            return answer

        if op == "credits":
            return _run(lambda: client.get_credits())
        if op == "account":
            return _run(lambda: client.get_account_info())

        if op == "enrich":
            budget.envoi()
            submitted = _run(lambda: client.submit_enrichment(
                finds, linkedin_url=linkedin_url, firstname=firstname,
                lastname=lastname, domain=domain, company_name=company_name,
                phone_type=phone_type, phone_country_codes=phone_country_codes),
                rc.is_platform)
            budget.reponses([submitted])
            # Nothing is spent until the lookup finishes: a TRACED zero, not an absence.
            if rc.is_platform:
                session_org.note_call_trace(quantity=0, units=1)
            else:
                session_org.note_call_trace(quantity=0)
            ident = submitted.get("enrichment_id") if isinstance(submitted, dict) else None
            if not ident:
                raise _bad("Pipecorn accepted the lookup but returned no enrichment_id: "
                           f"nothing can be read back ({str(submitted)[:200]}).")
            return {"enrichment_id": ident, "status": "pending", "find": finds,
                    "next_step": (f"Call pipecorn(op='result', enrichment_id='{ident}', "
                                  f"find={finds}) in a few seconds; reads are free.")}

        if not enrichment_id:
            raise _bad("op='result' needs the `enrichment_id` the submission returned.")
        answer = _run(lambda: client.get_enrichment(enrichment_id), rc.is_platform)
        status = answer.get("status") if isinstance(answer, dict) else None
        refacture = budget.rang_refacture(rc.mode, rc.entity_id)
        if status != "finished":
            if rc.is_platform:
                session_org.note_call_trace(quantity=0, units=0)
            else:
                session_org.note_call_trace(quantity=0)
        if status == "failed":
            if refacture:
                # No result will come: what the submission reserved is released.
                credits_releve.liberer_releve(*_releve(enrichment_id))
            code = answer.get("code") or ""
            why = _FAILED.get(code, f"Pipecorn stopped it ({code or 'no reason given'})")
            raise _bad(f"Pipecorn: lookup {enrichment_id} failed — {why}; nothing was "
                       "found or billed. Top up (or have the limit raised), then submit "
                       "it again.")
        if status != "finished":
            return {"done": False, "enrichment_id": enrichment_id,
                    "status": status or "pending",
                    "next_step": "Still looking: read it again in a few seconds."}
        found = found_kinds(answer, finds)
        spent = credits_spent(answer, finds)
        trace = {"quantity": 1 if any(found.values()) else 0, "provider_credits": spent,
                 "found_work_emails": int(found.get("email", False)),
                 "found_personal_emails": 0,
                 "found_phones": int(found.get("phone", False))}
        if rc.is_platform:
            trace["units"] = 0
        session_org.note_call_trace(**trace)
        if refacture:
            # The hold of the submission settles kind by kind at what was FOUND.
            credits_releve.regler_releve(
                *_releve(enrichment_id),
                {_NATURES[k]: int(hit) for k, hit in found.items()})
        # A third party's personal email never leaves here, even one Pipecorn returns
        # unasked (`pipecorn_personal_email_refused`).
        answer = {k: v for k, v in answer.items() if not k.startswith("personal_email")}
        return {**answer, "done": True, "pipecorn_credits": spent}
