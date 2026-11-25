"""Apify — the most common actors, each as its own tool.

`apify_run_sync(actor_id, run_input)` reaches the whole Store, but it says nothing
about WHAT it can do: an agent (or a user reading the connector page) sees "run an
actor" and has to know the actor's name and its input JSON. A handful of actors —
LinkedIn profiles, company employees and pages, website contacts, job boards,
LinkedIn posts, Google Maps — cover most needs. Each gets a tool here: a name that
says what it does, typed arguments, and the actor's input built for you.

Every tool runs through `apify.lancer_acteur` — the same start → wait → bill → read
path as `apify_run_sync`, so the run's real cost is traced the same way, once per run,
and the answer carries the same receipt (`apify_recu`: run id, dataset id, status, cost).
Every tool also caps the run: `max_items` (billed results) and `max_total_charge_usd`
(hard ceiling, $5 by default, refused above the bound of the launch — `apify._plafond`,
the one check of every launch, `apify_run_sync` included) — raise them for a big batch,
never drop them.

The actor's own input field names are pinned here, read from each actor's input
schema (`GET /v2/acts/<actor>/builds/default`, 25/10/2026). An actor that renames a
field breaks only its own tool: `apify_run_sync` with the new input still works.

Anything else in the Store: `apify_store_search` then `apify_run_sync`.
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, urlsplit

from fastmcp import FastMCP

from .apify import _bad, lancer_acteur

# The actors, pinned by `username/name` — what the Store shows and what we run.
LINKEDIN_PROFILES = "harvestapi/linkedin-profile-scraper"
LINKEDIN_EMPLOYEES = "harvestapi/linkedin-company-employees"
LINKEDIN_COMPANIES = "harvestapi/linkedin-company"
LINKEDIN_POSTS = "harvestapi/linkedin-post-search"
LINKEDIN_JOBS = "curious_coder/linkedin-jobs-scraper"
INDEED_JOBS = "valig/indeed-jobs-scraper"
WTTJ_JOBS = "bebity/welcome-to-the-jungle-jobs-scraper"
WEBSITE_CONTACTS = "vdrmota/contact-info-scraper"
WEBSITE_CRAWL = "apify/website-content-crawler"
GOOGLE_MAPS_PLACES = "compass/crawler-google-places"
GOOGLE_MAPS_REVIEWS = "compass/Google-Maps-Reviews-Scraper"

# The default hard ceiling of one run, in US dollars: the actors' list prices put a
# normal batch far below it, $5 only stops a run gone wrong. Its bound is the org's
# ceiling (within the platform's), checked by `lancer_acteur` for every launch.
_PLAFOND_USD = 5.0

# `profileScraperMode` takes the actor's display labels, prices included — the
# exact strings of its input schema (25/10/2026).
_MODE_PROFIL = {
    False: "Profile details no email ($4 per 1k)",
    True: "Profile details + email search ($10 per 1k)",
}
_MODE_EMPLOYES = {
    "short": "Short ($4 per 1k)",
    "full": "Full ($8 per 1k)",
    "full_email": "Full + email search ($12 per 1k)",
}


def _sans_vides(d: dict) -> dict:
    """The actor input without the arguments left unset: an absent field takes the
    actor's own default, an explicit `null` may not."""
    return {k: v for k, v in d.items() if v is not None and v != []}


def _au_moins_un(**listes: Optional[list]) -> None:
    if not any(listes.values()):
        raise _bad(f"Give at least one of: {', '.join(f'`{k}`' for k in listes)}.")


# --- Welcome to the Jungle: one row per offer, a lean row by default ---------------
#
# The WTTJ actor (`includeDetails` on by default) can emit the same offer several
# times in one run, and every row embeds the hiring company's whole profile —
# `organization` alone carries ~90 nested keys (cover images in five sizes, equality
# indexes, GDPR texts, social links…), on top of each body in three renderings (HTML,
# Markdown, text) and labels in five languages. Read raw, a 50-job run fills an
# agent's context with the same company profile 50 times.
#
# The key of an offer: `reference`, the WTTJ reference of the job — present from the
# search index on every row, details or not. `wttj_reference` comes only with the
# job details; `objectID` is a search-index record id, `publicUrl` carries the
# listing's locale (`/fr/`, `/en/`): both are fallbacks. A row with none of them is
# kept, never merged with another.
_WTTJ_CLES = ("reference", "wttj_reference", "objectID", "publicUrl")
_WTTJ_LOCALE = re.compile(r"^(https?://[^/]+)/[a-z]{2}(?:-[a-z]{2})?/", re.I)

# Each body ships three times: one is kept, the most readable present, under its own
# key — `<body>_text`, else `<body>_markdown`, else `<body>` (the HTML).
_WTTJ_CORPS = ("description", "profile", "recruitment_process")
# Dropped from the default row — denylist: a key not named here passes through.
_WTTJ_RETIRES = frozenset({
    # the company's presentation, in three renderings: `organization` is summarised
    "company_description", "company_description_markdown", "company_description_text",
    # labels in five languages, kept as one label below or redundant with a code
    # (`sectors` and `sectors_name` go only when `organization` takes them over)
    "contract_type_names", "profession_name",
    # media, page furniture and the application form
    "logo", "cover_image", "social_image", "videos", "cta_content", "urls",
    "featured_page", "application_fields", "ats_questions", "benefits", "_geoloc",
    "recruiter_proposal", "is_default",
    # flags that only say whether the value next to them is set
    "has_contract_duration", "has_education_level", "has_experience_level_minimum",
    "has_salary_yearly_minimum",
})
# The search index and the job details both give these: the index copy goes when
# the details say the same.
_WTTJ_DOUBLONS = (("salary_minimum", "salary_min"), ("salary_maximum", "salary_max"),
                  ("contract_duration_minimum", "contract_duration_min"),
                  ("contract_duration_maximum", "contract_duration_max"))
_WTTJ_BUREAU = ("address", "city", "zip_code", "country_code")


def _wttj_cle(job: Any) -> Optional[tuple]:
    if not isinstance(job, dict):
        return None
    for champ in _WTTJ_CLES:
        v = job.get(champ)
        if isinstance(v, (str, int)) and str(v).strip():
            v = str(v).strip()
            if champ == "publicUrl":
                v = _WTTJ_LOCALE.sub(r"\1/", v).rstrip("/").lower()
            return champ, v
    return None


def _wttj_dedoublonne(jobs: list) -> list:
    """The offers in their order, each once — the first row seen wins."""
    vus: set = set()
    out = []
    for job in jobs:
        cle = _wttj_cle(job)
        if cle is not None:
            if cle in vus:
                continue
            vus.add(cle)
        out.append(job)
    return out


def _libelle(v: Any) -> Any:
    """One label out of a `{en, fr, es, cs, sk}` dict: English, else French, else any."""
    if isinstance(v, dict):
        return v.get("en") or v.get("fr") or next(
            (x for x in v.values() if isinstance(x, str) and x), None)
    return v


def _noms(items: Any) -> Optional[list]:
    if not isinstance(items, list):
        return None
    noms = [_libelle(i.get("name")) if isinstance(i, dict) else i for i in items]
    return [n for n in noms if n] or None


def _bureau(o: Any) -> Any:
    if not isinstance(o, dict):
        return o
    return {k: o[k] for k in _WTTJ_BUREAU if o.get(k)} or None


def _vide(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _wttj_societe(org: Any, job: dict) -> Any:
    """The hiring company in a dozen keys — `full=True` returns its whole profile."""
    if not isinstance(org, dict):
        return org
    secteurs = []
    for s in job.get("sectors") or org.get("sectors") or []:
        if isinstance(s, dict):
            nom = _libelle(s.get("name"))
            parent = _libelle(s.get("parent") or s.get("parent_name"))
            if nom:
                secteurs.append(f"{parent} / {nom}" if parent else nom)
    siege = org.get("headquarter")
    societe = {
        "name": org.get("name"),
        "slug": org.get("slug"),
        "reference": org.get("reference"),
        "url": job.get("companyUrl"),
        "website": org.get("media_website_url"),
        "linkedin": org.get("media_linkedin"),
        "nb_employees": org.get("nb_employees"),
        "size": _libelle(org.get("size")),
        "creation_year": org.get("creation_year"),
        "revenue": org.get("revenue"),
        "jobs_count": org.get("jobs_count"),
        "industry": org.get("industry"),
        "sectors": secteurs,
        "headquarter": ({k: siege[k] for k in ("city", "country_code") if siege.get(k)}
                        if isinstance(siege, dict) else None),
    }
    return {k: v for k, v in societe.items() if not _vide(v)}


def _wttj_allege(job: Any) -> Any:
    """The default row of a WTTJ offer: the job's own facts, one rendering of each
    body, one label per field, and the company as a summary. Empty values go —
    nothing is lost, `full=True` returns the actor's row as is."""
    if not isinstance(job, dict):
        return job
    out = {k: v for k, v in job.items() if k not in _WTTJ_RETIRES}
    for corps in _WTTJ_CORPS:
        rendus = [(cle, out.pop(cle, None))
                  for cle in (f"{corps}_text", f"{corps}_markdown", corps)]
        cle, garde = next(((c, r) for c, r in rendus if r), (None, None))
        if garde:
            out[cle] = garde
    for index, detail in _WTTJ_DOUBLONS:
        if index in out and out.get(detail) == out[index]:
            del out[index]
    if "profession" in out:
        p = out["profession"]
        out["profession"] = (" / ".join(x for x in (
            _libelle(p.get("category") or p.get("category_name")),
            _libelle(p.get("name"))) if x) or None) if isinstance(p, dict) else p
    for cle in ("skills", "tools"):
        if cle in out:
            out[cle] = _noms(out[cle])
    if "office" in out:
        out["office"] = _bureau(out["office"])
    if isinstance(out.get("offices"), list):
        out["offices"] = [_bureau(o) for o in out["offices"]]
    if "organization" in out:
        out["organization"] = _wttj_societe(out["organization"], job)
        out.pop("companyUrl", None)   # now `organization.url`
        if isinstance(out["organization"], dict) and out["organization"].get("sectors"):
            out.pop("sectors", None)        # now `organization.sectors`
            out.pop("sectors_name", None)
    return {k: v for k, v in out.items() if not _vide(v)}


def _wttj_sortie(out: Any, *, full: bool, fields: Optional[list[str]]) -> Any:
    """Deduplicate the run's rows, then shape them: the caller's `fields` (the actor's
    own keys, as before), else the raw rows on `full`, else the lean row. Also inside
    the `{error, run_id, status, items}` answer of a run that did not succeed."""
    if isinstance(out, dict):
        if isinstance(out.get("items"), list):
            return {**out, "items": _wttj_sortie(out["items"], full=full, fields=fields)}
        return out
    if not isinstance(out, list):
        return out
    jobs = _wttj_dedoublonne(out)
    if fields:
        garde = set(fields)
        return [{k: v for k, v in j.items() if k in garde} if isinstance(j, dict) else j
                for j in jobs]
    if full:
        return jobs
    return [_wttj_allege(j) for j in jobs]


def register(mcp: FastMCP) -> None:

    # --- LinkedIn ------------------------------------------------------------

    @mcp.tool()
    def apify_linkedin_profiles(
        urls: list[str],
        with_email: bool = False,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Full LinkedIn profiles from their URLs — current title and company,
        experience, education, skills, location. No LinkedIn login needed.

        harvestapi/linkedin-profile-scraper: check one person's current job, or
        refresh the titles of a list. About $4 per 1,000 profiles,
        $10 with the email search.

        Returns — the LIST of profiles, one per URL found (or `{error, run_id, status,
            items, receipt}` for a run that did not succeed — billed too).

        Args:
            urls: profile URLs (`https://www.linkedin.com/in/<id>`) or bare public
                identifiers. One run takes a batch: send them together.
            with_email: also search a professional email (2.5× the price).
            fields: keep only these keys of each profile (they are wide), e.g.
                `["firstName", "lastName", "headline", "currentPosition", "linkedinUrl"]`.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        if not urls:
            raise _bad("Give at least one profile URL in `urls`.")
        return lancer_acteur(
            LINKEDIN_PROFILES,
            {"queries": urls, "profileScraperMode": _MODE_PROFIL[with_email]},
            outil="apify_linkedin_profiles", max_items=len(urls), fields=fields,
            max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_linkedin_company_employees(
        companies: list[str],
        job_titles: Optional[list[str]] = None,
        locations: Optional[list[str]] = None,
        search_query: Optional[str] = None,
        profile_mode: Literal["short", "full", "full_email"] = "short",
        max_items: int = 25,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """The people working at a company, from its LinkedIn page — with filters on
        title and location. No LinkedIn login needed.

        Use it when Apollo / AI Ark miss people (small companies, non-US, recent
        hires): titles come straight from LinkedIn and are often fresher.
        harvestapi/linkedin-company-employees.

        Keep only rows whose `currentPosition[0].companyUniversalName` is the target:
        LinkedIn search can return people from namesakes.

        Returns — the LIST of employee profiles (or `{error, run_id, status, items, receipt}`).

        Args:
            companies: company page URLs (`https://www.linkedin.com/company/<slug>`).
                Only have a name or a domain? Resolve it first with
                `apify_linkedin_companies(names=[…])`.
            job_titles: keep people with these titles (e.g. `["CEO", "Head of Marketing"]`).
            locations: keep people in these places, spelled out (`"United Kingdom"`,
                not `"UK"` — LinkedIn reads that as Ukraine).
            search_query: free-text fuzzy search on the employees.
            profile_mode: `short` (~$4/1k, title and company — enough to pick people),
                `full` (~$8/1k, full profile), `full_email` (~$12/1k, + email search).
                Pick people in `short`, then enrich only those you keep.
            max_items: profiles to return in all — a big company has thousands.
            fields: keep only these keys of each profile.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        if not companies:
            raise _bad("Give at least one LinkedIn company URL in `companies`.")
        return lancer_acteur(
            LINKEDIN_EMPLOYEES,
            _sans_vides({"companies": companies, "jobTitles": job_titles,
                         "locations": locations, "searchQuery": search_query,
                         "profileScraperMode": _MODE_EMPLOYES[profile_mode],
                         "maxItems": max_items}),
            outil="apify_linkedin_company_employees", max_items=max_items,
            fields=fields, max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_linkedin_companies(
        urls: Optional[list[str]] = None,
        names: Optional[list[str]] = None,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """LinkedIn company pages — description, website, industry, headcount,
        locations, followers — from their URLs, or found by company name.

        Also how you find a company's LinkedIn URL from its name. A name search
        returns the best match, which can be a namesake: check the returned
        `website` against the company's domain. harvestapi/linkedin-company.

        Returns — the LIST of company pages (or `{error, run_id, status, items, receipt}`).

        Args:
            urls: company page URLs (`https://www.linkedin.com/company/<slug>`).
            names: company names to search (short names work best: `"Acme"`, not
                `"Acme Deutschland GmbH"`).
            fields: keep only these keys of each company.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        _au_moins_un(urls=urls, names=names)
        return lancer_acteur(
            LINKEDIN_COMPANIES, _sans_vides({"companies": urls, "searches": names}),
            outil="apify_linkedin_companies",
            max_items=len(urls or []) + len(names or []), fields=fields,
            max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_linkedin_posts(
        queries: Optional[list[str]] = None,
        author_urls: Optional[list[str]] = None,
        posted_limit: Optional[Literal["1h", "24h", "week", "month", "3months",
                                       "6months", "year"]] = None,
        sort_by: Optional[Literal["relevance", "date"]] = None,
        max_posts: int = 20,
        with_reactions: bool = False,
        with_comments: bool = False,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """LinkedIn posts matching a search, or published by given people or
        companies — text, author, date, reactions and comment counts. No LinkedIn
        login needed. harvestapi/linkedin-post-search.

        Benchmark what performs on a topic, watch what a competitor or a prospect
        posts, or find who talks about a subject.

        Returns — the LIST of posts (or `{error, run_id, status, items, receipt}`).

        Args:
            queries: searches, as typed in LinkedIn's search bar (`"revops agency"`).
            author_urls: profile or company URLs whose posts (and reposts) you want.
            posted_limit: only posts newer than this.
            sort_by: `relevance` or `date`.
            max_posts: posts per query or author.
            with_reactions / with_comments: also scrape who reacted / commented
                (more items, more cost).
            fields: keep only these keys of each post.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        _au_moins_un(queries=queries, author_urls=author_urls)
        sources = len(queries or []) + len(author_urls or [])
        return lancer_acteur(
            LINKEDIN_POSTS,
            _sans_vides({"searchQueries": queries, "authorUrls": author_urls,
                         "postedLimit": posted_limit, "sortBy": sort_by,
                         "maxPosts": max_posts,
                         "scrapeReactions": with_reactions or None,
                         "scrapeComments": with_comments or None}),
            outil="apify_linkedin_posts",
            # Reactions and comments are items of their own: no cap on top of the
            # posts' one when they are asked, the dollar ceiling still holds.
            max_items=None if (with_reactions or with_comments) else sources * max_posts,
            fields=fields, max_total_charge_usd=max_total_charge_usd)

    # --- job postings --------------------------------------------------------

    # Measured 23/11/2026 (signal #1945): a URL carrying `geoId`, `f_TPR` and `f_F=mrkt`
    # gave 60 jobs in the right area and window, nearly none in marketing, and no job
    # in the dataset carries a job-function field to filter on afterwards. The public
    # (logged-out) search behind the actor drops the job-function filter; billing each
    # unfiltered job while the caller believes it filtered is a wrong answer with no
    # error, hence the refusal by name.
    _FILTRES_IGNORES_JOBS = {"f_F": "job function"}

    @mcp.tool()
    def apify_linkedin_jobs(
        keywords: Optional[str] = None,
        location: Optional[str] = None,
        search_urls: Optional[list[str]] = None,
        company_ids: Optional[list[str]] = None,
        date_posted: Optional[Literal["anyTime", "past24Hours", "pastWeek",
                                      "pastMonth"]] = None,
        with_company: bool = True,
        max_items: int = 50,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Live job postings from LinkedIn Jobs — title, company, location, date,
        description, applicants, the job poster and company details.
        curious_coder/linkedin-jobs-scraper.

        Hiring signals: who recruits for which role, where, since when. For
        structured, deduplicated postings across boards, try `theirstack_jobs_search`
        first.

        Returns — the LIST of jobs (or `{error, run_id, status, items, receipt}`).

        Args:
            keywords: job title or skills (`"head of growth"`).
            location: as typed on LinkedIn (`"France"`, `"London, United Kingdom"`).
            search_urls: public LinkedIn jobs search URLs, built on
                linkedin.com/jobs — used instead of `keywords`/`location`.
                The job-function filter (`f_F=`) is NOT applied by this
                scraper and is refused: narrow with keywords instead.
            company_ids: LinkedIn numeric company ids (`f_C=` in a search URL).
            date_posted: freshness filter.
            with_company: add the hiring company's details to each job.
            max_items: jobs per search.
            fields: keep only these keys of each job.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        if not (keywords or search_urls or company_ids):
            raise _bad("Give `keywords`, `search_urls` or `company_ids`.")
        ignores = sorted({f for u in (search_urls or [])
                          for f in _FILTRES_IGNORES_JOBS if f in parse_qs(urlsplit(u).query)})
        if ignores:
            raise _bad(
                "This scraper reads LinkedIn's public job search, which does not apply "
                f"{', '.join(f'`{f}` ({_FILTRES_IGNORES_JOBS[f]})' for f in ignores)}: "
                "the run would return jobs of every kind, billed. Remove it from the URL "
                "and narrow with `keywords` (e.g. \"marketing\") or the `keywords=` of the "
                "URL, then filter on each job's `title`. Location (`geoId`) and date "
                "(`f_TPR`) filters are applied.")
        sources = len(search_urls) if search_urls else 1
        return lancer_acteur(
            LINKEDIN_JOBS,
            _sans_vides({"urls": search_urls, "keywords": keywords,
                         "location": location, "companyIds": company_ids,
                         "datePosted": date_posted, "scrapeCompany": with_company,
                         "limitPerSource": max_items}),
            outil="apify_linkedin_jobs", max_items=sources * max_items, fields=fields,
            max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_indeed_jobs(
        title: str,
        location: Optional[str] = None,
        country: str = "us",
        date_posted: Optional[Literal["1", "3", "7", "14"]] = None,
        max_items: int = 50,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Live job postings from Indeed — title, company, location, salary, date,
        description. Very cheap per job. valig/indeed-jobs-scraper.

        Returns — the LIST of jobs (or `{error, run_id, status, items, receipt}`).

        Args:
            title: the job title to search (`"sales manager"`).
            location: city or region.
            country: Indeed country, ISO 3166-1 alpha-2 lowercase (`"fr"`, `"gb"`, `"us"`).
            date_posted: only jobs posted in the last N days.
            max_items: jobs to return.
            fields: keep only these keys of each job.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        return lancer_acteur(
            INDEED_JOBS,
            _sans_vides({"title": title, "location": location,
                         "country": country.lower(), "datePosted": date_posted,
                         "limit": max_items}),
            outil="apify_indeed_jobs", max_items=max_items, fields=fields,
            max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_welcome_to_the_jungle_jobs(
        query: Optional[str] = None,
        company_slugs: Optional[list[str]] = None,
        country_code: Optional[str] = None,
        city: Optional[str] = None,
        posted_within_days: Optional[int] = None,
        contract_types: Optional[list[Literal[
            "FULL_TIME", "PART_TIME", "INTERNSHIP", "APPRENTICESHIP", "FREELANCE",
            "TEMPORARY", "GRADUATE_PROGRAM", "VIE", "VOLUNTEER", "OTHER"]]] = None,
        max_items: int = 50,
        fields: Optional[list[str]] = None,
        full: bool = False,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Live job postings from Welcome to the Jungle (France and Europe) — title,
        company, contract, remote policy, salary, full description.
        bebity/welcome-to-the-jungle-jobs-scraper.

        Each offer comes back once (by its WTTJ `reference`), so a run can return
        fewer jobs than `max_items`. The default row keeps one rendering of each text
        (`description_text`, else `description_markdown`, else the HTML `description`;
        same for `profile`, `recruitment_process`), one label per field, and the
        company as a summary in `organization` (name, slug, url, website, size,
        creation_year, revenue, jobs_count, sectors, headquarter); `full=True` returns
        the actor's rows whole, the company's full profile included.

        Returns — the LIST of jobs (or `{error, run_id, status, items, receipt}`).

        Args:
            query: free-text search (`"business developer"`).
            company_slugs: only these companies' jobs — the slug of their WTTJ URL
                (`"doctolib"`) or the full company URL.
            country_code: ISO code of the office (`"FR"`, `"ES"`, `"GB"`).
            city: office city.
            posted_within_days: only listings published in the last N days.
            contract_types: keep these contracts.
            max_items: jobs to return.
            fields: keep only these keys of each job, as the actor names them
                (`["name", "publicUrl", "organization"]`) — the raw values.
            full: the actor's rows as is, company profile and every rendering.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        if not (query or company_slugs):
            raise _bad("Give `query` or `company_slugs`.")
        # The keys of an offer travel with the caller's `fields`, so the rows can be
        # deduplicated; `_wttj_sortie` then keeps only the keys asked for.
        lus = list(dict.fromkeys([*fields, *_WTTJ_CLES])) if fields else None
        out = lancer_acteur(
            WTTJ_JOBS,
            _sans_vides({"action": "get-jobs", "query": query,
                         "companySlugs": company_slugs, "countryCode": country_code,
                         "city": city, "postedWithinDays": posted_within_days,
                         "contractType": contract_types, "maxItems": max_items}),
            outil="apify_welcome_to_the_jungle_jobs", max_items=max_items,
            fields=lus, max_total_charge_usd=max_total_charge_usd)
        return _wttj_sortie(out, full=full, fields=fields)

    # --- websites ------------------------------------------------------------

    @mcp.tool()
    def apify_website_contacts(
        urls: list[str],
        max_depth: int = 1,
        max_pages_per_site: int = 4,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Contact details published on company websites — emails, phones, and
        LinkedIn / Facebook / Instagram / X / YouTube links. One merged row per site.
        vdrmota/contact-info-scraper.

        Find a company's LinkedIn page or generic email from its website. ~$0.30 for
        50 sites with the defaults. More than ~30 sites can exceed the 300 s wait:
        the run keeps going, pick it up with `apify_run_status` then
        `apify_dataset_items` (the error names the run).

        Returns — the LIST of sites with `emails`, `phones`, `linkedIns`… (or
            `{error, run_id, status, items, receipt}`).

        Args:
            urls: website URLs (`https://www.example.com`).
            max_depth: link depth from each start page (1 = the home page and the
                pages it links to — contact, about…).
            max_pages_per_site: pages read per site at most.
            fields: keep only these keys, e.g. `["domain", "emails", "linkedIns"]`.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        if not urls:
            raise _bad("Give at least one website URL in `urls`.")
        return lancer_acteur(
            WEBSITE_CONTACTS,
            {"startUrls": [{"url": u} for u in urls], "maxDepth": max_depth,
             "maxRequestsPerStartUrl": max_pages_per_site, "sameDomain": True,
             "mergeContacts": True, "proxyConfig": {"useApifyProxy": True}},
            # One merged row per site: as many billed results as sites.
            outil="apify_website_contacts", max_items=len(urls), fields=fields,
            max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_website_crawl(
        urls: list[str],
        max_pages: int = 10,
        max_depth: Optional[int] = None,
        include_globs: Optional[list[str]] = None,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Crawl a website and return the text of its pages as Markdown — for
        reading a whole site (docs, product pages, a blog), not one page.
        apify/website-content-crawler.

        One page only? `web_read` is free and faster.

        Returns — the LIST of pages with `url`, `markdown`, `metadata` (or
            `{error, run_id, status, items, receipt}`).

        Args:
            urls: start URLs; the crawl stays under each one's path.
            max_pages: pages to crawl in all.
            max_depth: link depth from the start URLs.
            include_globs: only crawl URLs matching these globs
                (`["https://example.com/blog/**"]`).
            fields: keep only these keys, e.g. `["url", "markdown"]`.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        if not urls:
            raise _bad("Give at least one start URL in `urls`.")
        return lancer_acteur(
            WEBSITE_CRAWL,
            _sans_vides({"startUrls": [{"url": u} for u in urls],
                         "maxCrawlPages": max_pages, "maxResults": max_pages,
                         "maxCrawlDepth": max_depth,
                         "includeUrlGlobs": [{"glob": g} for g in include_globs or []],
                         "proxyConfiguration": {"useApifyProxy": True}}),
            outil="apify_website_crawl", max_items=max_pages, fields=fields,
            max_total_charge_usd=max_total_charge_usd)

    # --- Google Maps ---------------------------------------------------------

    @mcp.tool()
    def apify_google_maps_places(
        search_terms: list[str],
        location: str,
        max_places: int = 20,
        language: str = "en",
        with_contacts: bool = False,
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Businesses on Google Maps for a search in an area — name, address, phone,
        website, category, rating, review count, opening hours.
        compass/crawler-google-places.

        For a quick look, `serper_maps_sample` is cheaper; use this one for full
        place records or a whole city/region (it splits big areas for you).

        Returns — the LIST of places (or `{error, run_id, status, items, receipt}`).

        Args:
            search_terms: what to search (`["bakery", "pastry shop"]`).
            location: one place in free text, simple is best (`"Marseille, France"`).
            max_places: places per search term.
            language: language of the results (`"fr"`, `"en"`).
            with_contacts: also read each business website for emails and social
                links (paid add-on).
            fields: keep only these keys, e.g. `["title", "phone", "website", "address"]`.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        if not search_terms:
            raise _bad("Give at least one search term in `search_terms`.")
        return lancer_acteur(
            GOOGLE_MAPS_PLACES,
            _sans_vides({"searchStringsArray": search_terms, "locationQuery": location,
                         "maxCrawledPlacesPerSearch": max_places, "language": language,
                         "scrapeContacts": with_contacts or None}),
            outil="apify_google_maps_places",
            max_items=len(search_terms) * max_places, fields=fields,
            max_total_charge_usd=max_total_charge_usd)

    @mcp.tool()
    def apify_google_maps_reviews(
        place_urls: Optional[list[str]] = None,
        place_ids: Optional[list[str]] = None,
        max_reviews: int = 50,
        sort: Literal["newest", "mostRelevant", "highestRanking",
                      "lowestRanking"] = "newest",
        since: Optional[str] = None,
        language: str = "en",
        fields: Optional[list[str]] = None,
        max_total_charge_usd: float = _PLAFOND_USD,
    ) -> Any:
        """Google Maps reviews of given places — text, rating, date, owner reply.
        compass/Google-Maps-Reviews-Scraper.

        Returns — the LIST of reviews (or `{error, run_id, status, items, receipt}`).

        Args:
            place_urls: Google Maps place URLs (containing `/maps/place/`).
            place_ids: Google place ids (`ChIJ…`), e.g. from
                `apify_google_maps_places`.
            max_reviews: reviews per place.
            sort: review order.
            since: only reviews newer than this — a date (`"2026-01-01"`) or a
                span (`"3 months"`).
            language: language of the reviews' UI text.
            fields: keep only these keys, e.g. `["stars", "text", "publishedAtDate"]`.
            max_total_charge_usd: hard cost ceiling of the run, $5 by default, at most
                your organization's ceiling per run ($25 unless set otherwise).
        """
        _au_moins_un(place_urls=place_urls, place_ids=place_ids)
        places = len(place_urls or []) + len(place_ids or [])
        return lancer_acteur(
            GOOGLE_MAPS_REVIEWS,
            _sans_vides({"startUrls": [{"url": u} for u in place_urls or []],
                         "placeIds": place_ids, "maxReviews": max_reviews,
                         "reviewsSort": sort, "reviewsStartDate": since,
                         "language": language}),
            outil="apify_google_maps_reviews", max_items=places * max_reviews,
            fields=fields, max_total_charge_usd=max_total_charge_usd)
