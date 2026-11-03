"""The six waterfall tools (`waterfall_email|phone|company|person|people|tech`) — one per
connector, all read-only: the cascade itself is the domain package `oto_mcp/waterfall/`
(`docs/waterfall.md`). Their table-writing `<ns>_act` is `tools/waterfall_lignes.py`."""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from ..waterfall.email import EMAIL_STEPS
from ..waterfall.entrees import _company_inputs, _people_inputs, _person_inputs, _tech_inputs
from ..waterfall.gens import PEOPLE_STEPS
from ..waterfall.moteur import _limits, _run_cascade
from ..waterfall.personne import PERSON_STEPS
from ..waterfall.qualification import _qualify_args
from ..waterfall.societe import COMPANY_STEPS
from ..waterfall.tech import TECH_STEPS
from ..waterfall.telephone import PHONE_STEPS
from ..waterfall.vivier import _slim_people
from .lecture import LECTURE, documente

_ORDER_DOC = """**Order**: your `steps` setting on this connector's card if set (an org
        default, or your own override), kept as written; else the default order above,
        where the free steps run first, then lemlist's database on your OWN lemlist key
        (no credits, your own quota), then the other providers you hold your own key
        for, and the shared keys next. A provider with no usable key is skipped without
        being called. Each step
        uses THAT provider's key and is billed by it, exactly as a direct call of its
        tool — the waterfall adds no cost. A step whose inputs are missing, or whose
        connector is not activated for this org, is skipped too. Give every input you
        have. One contact per call; for a list, run it from a recipe (`for_each`)."""

_RECEIPT_DOC = """`attempts` says, step by step, what happened (found / no_match /
        skipped / failed / withheld / pending, with the code, the message and the key
        used); `order_from` says where the order came from (call / setting / default);
        `pending` gives the collect call of the asynchronous step the cascade stopped
        at."""

_QUALIFY_ARGS = """            qualify: a Jev rubric to judge each result against your target, in ONE
                cheap call (micro-dollars, on the shared Jev key) — instead of reading
                every result yourself. Same shape as `jev_ask`'s `questions`, e.g.
                `{"fit": {"type": "noul", "instructions": "Is this a buyer of …?",
                "criteria": {"true": "…", "false": "…"}}}`. Only the fields a fit needs
                go to Jev (title, seniority, industry, size…), never a name, email,
                phone or profile URL. Each result gets `qualification: {answers,
                model}`. No usable Jev key → the call is REFUSED before anything is
                paid (`qualification_unavailable`); no time left → results come back
                unqualified, the receipt's `qualification` says why. Qualify BEFORE you
                pay to reveal or enrich anyone.
            qualify_min: keep only results whose yes/no (`noul`) answers are all at or
                above this probability (e.g. 0.7). Omitted = nothing dropped, every
                result annotated. A probability near 0.5 means "as likely as not". For
                PEOPLE, each provider's finds are judged as they come: a provider whose
                people ALL fall short does not end the search — the next one is tried —
                and each attempt says `qualified` / `not_qualified`. A company or a
                person stays, with `qualification.passed`.
            (People: Apollo's FREE search, names masked, is read on EVERY page (up to
                1,000 people, while time allows — `pages` / `pool_truncated` say so), judged
                on its titles, and only the best `limit` that pass are revealed — 1 Apollo credit each, with
                the full name, LinkedIn URL and Apollo's email — before any paid
                provider is tried.)
            show_unqualified: the people who did not pass are ALWAYS returned under
                `unqualified` — name, title, profile URL and Jev's answers, to check the
                rubric without paying again; true returns their FULL records instead.
                Continuing past an unqualified provider pays off when it found FEW
                people; when the company simply has no fit, the next providers find the
                same people again (already-rejected ones are not re-judged) —
                `max_paid_steps` is the brake. A PAID list (Google, AI Ark) short of `limit`
                qualified people buys its NEXT pages, one at a time, each judged, until
                `limit` pass or its results end (`pages`, `stopped` on the attempt; each
                page counts in `max_paid_steps`). Still short, the search STOPS at that
                provider and `continue_with` names the providers not tried yet: yours to
                call (with `providers=`) if more are worth paying for."""

_COMMON_ARGS = """            providers: the steps to try for THIS call, in this order — keys
                from the list above. Overrides your setting. Never a tool name.
            dry_run: return the plan (order, key per step, arguments, skips) without
                calling anything. `planned` is not a promise: the connector's activation
                for your org is only checked by the real call.
            max_paid_steps: at most this many PAID provider calls for this lookup (free
                steps don't count); the steps past it are skipped. Omitted = no limit —
                the default: set it when spend matters more than finding.
            apify_max_usd: a cost ceiling for each Apify run of this lookup (≥ 0.50 —
                Apify refuses less; above your organization's ceiling per run, $25
                unless its admins set another, the step is refused). Omitted = the
                ceiling Apify launches take by default: derived from the run's size,
                else the platform default ($5), and never above that ceiling."""


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=LECTURE)
    @documente(f"""Find ONE person's professional email, trying providers in turn and
        stopping at the FIRST that returns one (native waterfall).

        Steps (default order): hunter (name + domain) → aiark (LinkedIn URL) → apollo
        (LinkedIn URL, or name + domain / company) → kaspr (LinkedIn URL) → lusha (LinkedIn
        URL, or name + domain / company) → forager (LinkedIn URL — your own Forager key,
        or the shared one where this instance opens email lookups) → then the asynchronous ones: dropcontact (LinkedIn URL, or name + company /
        domain) → lemlist (LinkedIn URL, or name + domain) → fullenrich (LinkedIn URL, or
        name + domain — usually 30 s to 4 min, so often `pending`). A slow step is billed
        even when its answer comes back `pending`, and a `pending` step ENDS the cascade:
        nothing after it is paid. The email is NOT verified here.

        {_ORDER_DOC}

        Returns `{{found, provider, email: {{email, all, status?}}, attempts,
        order_from}}` — {_RECEIPT_DOC}

        Args:
            first_name: the person's first name.
            last_name: the person's last name.
            full_name: alternative to first_name + last_name.
            domain: the company's domain (`acme.com`; a URL is accepted).
            company_name: the company's name, when the domain is unknown.
            linkedin_url: the person's LinkedIn profile URL.
{_COMMON_ARGS}
        """)
    async def waterfall_email(
        first_name: Optional[str] = None, last_name: Optional[str] = None,
        full_name: Optional[str] = None, domain: Optional[str] = None,
        company_name: Optional[str] = None, linkedin_url: Optional[str] = None,
        providers: Optional[list[str]] = None, dry_run: bool = False,
        max_paid_steps: Optional[int] = None, apify_max_usd: Optional[float] = None,
    ) -> dict:
        inputs = _person_inputs(first_name, last_name, full_name, domain,
                                company_name, linkedin_url, None)
        return await _run_cascade(mcp, "waterfall_email", "email", EMAIL_STEPS, inputs,
                                  providers, dry_run,
                                  _limits(max_paid_steps, apify_max_usd))

    @mcp.tool(annotations=LECTURE)
    @documente(f"""Find ONE person's phone number, trying providers in turn and stopping at
        the FIRST that returns one (native waterfall).

        Steps (default order): pipecorn (LinkedIn URL; its own cascade of providers,
        mobiles only, billed only when one is found, even when another step found too —
        asynchronous, waited up to 20 s within the call's time budget, then the next
        steps run and it is read once more at the end if time is left, else left in
        `pending`) → kaspr (LinkedIn URL) →
        lusha (LinkedIn URL, or name + domain / company) → forager (LinkedIn URL) → then
        the asynchronous ones: apollo
        (phone reveal, your own Apollo key; LinkedIn URL, or name + domain / company) →
        lemlist (LinkedIn URL, or name + domain) → dropcontact (LinkedIn URL, or name +
        company / domain) → fullenrich (LinkedIn URL, or name + domain — usually 30 s to
        4 min, so often `pending`). A slow step is billed even when its answer comes back
        `pending`, and a `pending` step ENDS the cascade: nothing after it is paid. The
        LinkedIn URL opens the most steps. Phone credits cost more than email credits.

        {_ORDER_DOC}

        Returns `{{found, provider, phone: {{phone, all}}, attempts, order_from}}` —
        {_RECEIPT_DOC}

        Args:
            linkedin_url: the person's LinkedIn profile URL.
            first_name: the person's first name.
            last_name: the person's last name.
            full_name: alternative to first_name + last_name.
            domain: the company's domain (`acme.com`; a URL is accepted).
            company_name: the company's name, when the domain is unknown.
{_COMMON_ARGS}
        """)
    async def waterfall_phone(
        linkedin_url: Optional[str] = None, first_name: Optional[str] = None,
        last_name: Optional[str] = None, full_name: Optional[str] = None,
        domain: Optional[str] = None, company_name: Optional[str] = None,
        providers: Optional[list[str]] = None, dry_run: bool = False,
        max_paid_steps: Optional[int] = None, apify_max_usd: Optional[float] = None,
    ) -> dict:
        inputs = _person_inputs(first_name, last_name, full_name, domain,
                                company_name, linkedin_url, None)
        return await _run_cascade(mcp, "waterfall_phone", "phone", PHONE_STEPS, inputs,
                                  providers, dry_run,
                                  _limits(max_paid_steps, apify_max_usd))

    @mcp.tool(annotations=LECTURE)
    @documente(f"""Find ONE company's firmographics (legal name, SIREN, domain, city,
        industry / NAF, size, revenue, founding date, LinkedIn), trying sources in turn
        and stopping at the FIRST that identifies it (native waterfall).

        Steps (default order): fr (the French registry, free — by SIREN, else by name for a
        French company) → lemlist (domain — lemlist's company database: headcount, size
        band, industry, headquarters, LinkedIn page; no credits, one call of the key's
        search quota) → aiark (domain or name) → apollo (domain) →
        theirstack (domain) → cognism (domain or name); then, when none of them gave
        the company's LinkedIn page: lemlist_linkedin (lemlist's company database, when
        the lemlist step did not already answer) → linkedin (the LinkedIn links on its
        own website, else a LinkedIn company search by name, kept only if its website is
        the domain). A search result counts only if
        its domain matches, or its name contains the name asked: a namesake never stops
        the cascade. `employees_band` is the INSEE size band code, not a headcount.

        {_ORDER_DOC}

        Returns `{{found, provider, company: {{name, siren?, domain?, country?, city?,
        naf? | industry?, employees? | employees_band?, revenue?, founded?,
        linkedin_url?, type?, market?}}, attempts, order_from}}` — {_RECEIPT_DOC}

        Args:
            company_name: the company's name.
            domain: its website domain (`acme.com`; a URL is accepted).
            siren: its 9-digit SIREN (France).
            country: ISO country code; other than FR skips the registry's name search.
{_COMMON_ARGS}
{_QUALIFY_ARGS}
        """)
    async def waterfall_company(
        company_name: Optional[str] = None, domain: Optional[str] = None,
        siren: Optional[str] = None, country: Optional[str] = None,
        providers: Optional[list[str]] = None, dry_run: bool = False,
        max_paid_steps: Optional[int] = None, apify_max_usd: Optional[float] = None,
        qualify: Optional[dict] = None, qualify_min: Optional[float] = None,
        show_unqualified: bool = False,
    ) -> dict:
        inputs = _company_inputs(company_name, domain, siren, country)
        return await _run_cascade(mcp, "waterfall_company", "company", COMPANY_STEPS,
                                  inputs, providers, dry_run,
                                  _limits(max_paid_steps, apify_max_usd),
                                  _qualify_args(qualify, qualify_min, show_unqualified))

    @mcp.tool(annotations=LECTURE)
    @documente(f"""Find ONE person's current profile (title, company, seniority, location,
        LinkedIn), trying providers in turn and stopping at the FIRST that returns a
        title or a company (native waterfall).

        Steps (default order): linkedin (LinkedIn URL — your own connected LinkedIn
        account, free) → lemlist_db (LinkedIn URL, or name + domain — lemlist's people
        database: current title, company, seniority, location and a pattern-guessed
        email, `email_status="guessed"`; no credits, one call of the key's search quota)
        → aiark (LinkedIn URL) → apollo (LinkedIn URL, email, or name +
        domain / company) → forager (LinkedIn URL) → cognism (same as apollo) → lusha
        (same as apollo) → lemlist (LinkedIn URL, or name + domain — asynchronous).

        {_ORDER_DOC}

        Returns `{{found, provider, person: {{title?, company_name?, company_domain?,
        full_name?, headline?, seniority?, department?, city?, country?, location?,
        linkedin_url?, email?, email_status?}},
        attempts, order_from}}` — {_RECEIPT_DOC}

        Args:
            linkedin_url: the person's LinkedIn profile URL.
            email: the person's email.
            first_name: the person's first name.
            last_name: the person's last name.
            full_name: alternative to first_name + last_name.
            domain: the company's domain (`acme.com`; a URL is accepted).
            company_name: the company's name, when the domain is unknown.
{_COMMON_ARGS}
{_QUALIFY_ARGS}
        """)
    async def waterfall_person(
        linkedin_url: Optional[str] = None, email: Optional[str] = None,
        first_name: Optional[str] = None, last_name: Optional[str] = None,
        full_name: Optional[str] = None, domain: Optional[str] = None,
        company_name: Optional[str] = None,
        providers: Optional[list[str]] = None, dry_run: bool = False,
        max_paid_steps: Optional[int] = None, apify_max_usd: Optional[float] = None,
        qualify: Optional[dict] = None, qualify_min: Optional[float] = None,
        show_unqualified: bool = False,
    ) -> dict:
        inputs = _person_inputs(first_name, last_name, full_name, domain,
                                company_name, linkedin_url, email)
        return await _run_cascade(mcp, "waterfall_person", "person", PERSON_STEPS,
                                  inputs, providers, dry_run,
                                  _limits(max_paid_steps, apify_max_usd),
                                  _qualify_args(qualify, qualify_min, show_unqualified))

    @mcp.tool(annotations=LECTURE)
    @documente(f"""Find the PEOPLE working at ONE company — its decision-makers when you
        give roles — trying sources in turn and stopping at the FIRST that returns at
        least one matching person (native waterfall).

        Steps (default order): apollo (domain — a free search; last names come back
        MASKED, reveal one by its `apollo_id` with `apollo_match_person`, a paid match:
        masked names alone do NOT stop the cascade, they are its fallback) → lemlist
        (domain or LinkedIn page — lemlist's people database: full names, current titles,
        LinkedIn URLs and a pattern-guessed email, `email_status="guessed"`; no credits,
        each page one call of the key's search quota; FIRST when the lemlist key is
        your own or your org's) → google (name
        or domain — LinkedIn profiles found on Google, full names) → aiark (domain) →
        linkedin_employees (the company's LinkedIn page — current titles straight from
        LinkedIn, often fresher than the data vendors; get the page with
        `waterfall_company`). A person is kept only when their title matches one of `roles` (when
        given), and, for LinkedIn and Google, only when their CURRENT company is the
        one asked for: a company page or a search pulls in people from elsewhere
        (lemlist: a current position there; its own title filter serves `roles`).

        {_ORDER_DOC}

        Returns `{{found, provider, people: {{people: [{{full_name, title?,
        linkedin_url?, location?, apollo_id?}}], count}}, attempts, order_from}}` —
        {_RECEIPT_DOC}

        Args:
            domain: the company's domain (`acme.com`; a URL is accepted).
            company_name: the company's name.
            company_linkedin_url: the company's LinkedIn page (linkedin.com/company/…).
            roles: role keywords to keep, e.g. ["sales", "CEO", "head of marketing"]
                (or one comma-separated string). Omitted = everyone the step returns.
            country: where the people are (e.g. "France") — the filter for a local
                subsidiary of a global domain (Apollo).
            limit: how many people at most (default 10, up to 100) — AI Ark and the
                LinkedIn employees bill per person returned, so raise it knowingly.
{_COMMON_ARGS}
{_QUALIFY_ARGS}
            full: every field a source gave for each person, and Jev's answers in full.
                Default: who they are (name, title, LinkedIn URL, location, seniority),
                how to reach them (email, Apollo id) and Jev's verdict as `fit`.
        """)
    async def waterfall_people(
        domain: Optional[str] = None, company_name: Optional[str] = None,
        company_linkedin_url: Optional[str] = None,
        roles: Optional[list[str]] = None, country: Optional[str] = None,
        limit: int = 10,
        providers: Optional[list[str]] = None, dry_run: bool = False,
        max_paid_steps: Optional[int] = None, apify_max_usd: Optional[float] = None,
        qualify: Optional[dict] = None, qualify_min: Optional[float] = None,
        show_unqualified: bool = False, full: bool = False,
    ) -> dict:
        inputs = _people_inputs(company_name, domain, company_linkedin_url, roles,
                                country, limit)
        out = await _run_cascade(mcp, "waterfall_people", "people", PEOPLE_STEPS,
                                 inputs, providers, dry_run,
                                 _limits(max_paid_steps, apify_max_usd),
                                 _qualify_args(qualify, qualify_min, show_unqualified))
        return out if full else _slim_people(out)

    @mcp.tool(annotations=LECTURE)
    @documente(f"""Find ONE company's tech stack, trying sources in turn and stopping at
        the FIRST that returns it (native waterfall) — or, with `technologies`, at the
        first that detects one of them.

        Steps (default order): lemlist (domain — lemlist's company record: website and
        business tools, kept when lemlist scores them CONFIRMED or LIKELY; no credits,
        one call of the key's search quota) → dataforseo (domain — what the WEBSITE runs, from
        DataForSEO's index: CMS, framework, hosting, analytics, e-commerce, payment) →
        apify (domain — the same, by a live crawl of the site now: for a site the index
        has not seen) → apollo (domain — Apollo's company record: website AND business
        tools, ~100 names with categories) → aiark (domain — AI Ark's company record,
        the same kind of mix) → theirstack (domain or name — what the company's JOB POSTINGS
        name: CRM, ERP, data stack, internal tools; far more names, dearer, thin on small
        companies). The website sources give display names (`Next.js`), TheirStack gives
        slugs (`next-js`): `matched` compares them loosely.

        To know whether a company uses a given tool, pass it in `technologies`: a
        source that does not detect any of them does NOT stop the cascade (the next
        one sees other things), and `found: false` means none of the sources detected
        it — not that the company does not use it. To read back-office tools first,
        call with `providers=["theirstack"]`.

        {_ORDER_DOC}

        Returns `{{found, provider, tech: {{technologies, count, source ("website" |
        "vendor_database" | "job_postings"), by_category?, matched?}}, attempts, order_from}}` —
        {_RECEIPT_DOC}

        Args:
            domain: the company's website domain (`acme.com`; a URL is accepted).
            company_name: the company's name, when the domain is unknown (TheirStack
                only).
            technologies: the tools you are looking for (e.g. ["Shopify", "HubSpot"],
                or one comma-separated string). Omitted = the first stack found.
{_COMMON_ARGS}
        """)
    async def waterfall_tech(
        domain: Optional[str] = None, company_name: Optional[str] = None,
        technologies: Optional[list[str]] = None,
        providers: Optional[list[str]] = None, dry_run: bool = False,
        max_paid_steps: Optional[int] = None, apify_max_usd: Optional[float] = None,
    ) -> dict:
        inputs = _tech_inputs(company_name, domain, technologies)
        return await _run_cascade(mcp, "waterfall_tech", "tech", TECH_STEPS, inputs,
                                  providers, dry_run,
                                  _limits(max_paid_steps, apify_max_usd))
