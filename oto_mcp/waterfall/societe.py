"""The company cascade (`waterfall_company`): its steps, in their default order, how
each source's answer is read, and the namesake guard — a search always returns
something, and a namesake must never stop the cascade."""
from __future__ import annotations

from typing import Optional

from .etape import Step
from .fournisseurs import (_APIFY_TIMEOUT_S, _apify_items, _lemlist_company_args,
                           _lemlist_company_rec)
from .valeurs import _clean, _company_page, _domain, _find_key, _get, _pick, _same_company

def _fr_company(ident: dict) -> Optional[dict]:
    siege = ident.get("siege") or {}
    return _clean({
        "name": ident.get("nom_complet") or ident.get("nom_raison_sociale"),
        "siren": ident.get("siren"),
        "domain": _domain(ident.get("site_internet")),
        "country": "FR",
        "city": siege.get("libelle_commune"),
        "address": siege.get("adresse"),
        "naf": ident.get("activite_principale"),
        # An INSEE size BAND code, not a headcount — named as such.
        "employees_band": ident.get("tranche_effectif_salarie"),
        "category": ident.get("categorie_entreprise"),
        "founded": ident.get("date_creation"),
    }, ("name",))


def _fr_get_company(res, inputs):
    if not isinstance(res, dict) or res.get("error"):
        return None
    return _fr_company(res.get("identity") or {})




def _fr_search_company(res, inputs):
    for ident in (_get(res, "results") or [])[:5]:
        if not isinstance(ident, dict):
            continue
        site = _domain(ident.get("site_internet"))
        if inputs.get("domain") and site == inputs["domain"]:
            return _fr_company(ident)
        names = [ident.get(k) for k in ("nom_complet", "nom_raison_sociale", "sigle")]
        if any(_same_company({"company_name": inputs.get("company_name")}, n, None)
               for n in names if n):
            return _fr_company(ident)
    return None


def _apollo_company(res, inputs):
    org = _get(res, "organization")
    if not isinstance(org, dict):
        return None
    return _clean({
        "name": org.get("name"),
        "domain": _domain(org.get("primary_domain") or org.get("website_url")),
        "country": org.get("country"),
        "city": org.get("city"),
        "employees": org.get("estimated_num_employees"),
        "revenue": org.get("annual_revenue"),
        "industry": org.get("industry"),
        "founded": org.get("founded_year"),
        "linkedin_url": org.get("linkedin_url"),
    }, ("name",))


def _count(v) -> Optional[int]:
    """lemlist serves its figures as strings (`"454"`)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _lemlist_company(res, inputs):
    rec = _lemlist_company_rec(res, inputs)
    if rec is None:
        return None
    return _clean({
        "name": rec.get("company_name"),
        "domain": _domain(rec.get("company_domain") or rec.get("company_website_url")),
        "country": rec.get("company_headquarters_country"),
        "city": rec.get("company_headquarters_city"),
        "employees": _count(rec.get("company_employee_count")),
        # A size BAND (`201-500`), as lemlist gives it.
        "employees_band": rec.get("company_size"),
        "industry": (rec.get("company_subsubindustry") or rec.get("company_subindustry")
                     or rec.get("company_industry")),
        "founded": _count(rec.get("company_founded")),
        "linkedin_url": rec.get("company_linkedin_url"),
        "type": rec.get("company_type"),
        "market": rec.get("business_business_customer"),
    }, ("name",))


def _aiark_company_args(inputs: dict) -> dict:
    if inputs.get("domain"):
        account = {"domain": {"any": {"include": [inputs["domain"]]}}}
    else:
        account = {"name": {"any": {"include": {"mode": "SMART",
                                                "content": [inputs["company_name"]]}}}}
    # Billed PER PROFILE returned: a domain names one company (1); a name needs a few to
    # step over namesakes (3) — never more than the step can use.
    return {"op": "companies", "account": account,
            "size": 1 if inputs.get("domain") else 3}


def _aiark_company(res, inputs):
    for rec in (_get(res, "content") or [])[:3]:
        if not isinstance(rec, dict):
            continue
        name = _pick(rec, "summary.name", "name")
        domain = _domain(_pick(rec, "link.website", "link.domain", "summary.website",
                               "summary.domain", "website", "domain"))
        # A domain search returns the domain's company: no name to check. A name
        # search must name it.
        if not inputs.get("domain") and not _same_company(inputs, name, None):
            continue
        return _clean({
            "name": name,
            "domain": domain or inputs.get("domain"),
            "industry": _pick(rec, "summary.industry", "industry"),
            "employees": _pick(rec, "summary.staff.total", "staff.total"),
            "city": _pick(rec, "location.headquarter.city", "location.city"),
            "country": _pick(rec, "location.headquarter.country", "location.country"),
            "linkedin_url": _pick(rec, "link.linkedin"),
            "founded": _pick(rec, "summary.founded_year", "summary.founded"),
            # A RANGE ("100000-500000"), as AI Ark gives it — never a guessed figure.
            "revenue_range": _pick(rec, "financial.revenue.annual.amount"),
            "phone": _pick(rec, "contact.phone.sanitized", "contact.phone.raw"),
        }, ("name",))
    return None


def _pipecorn_company(res, inputs):
    """Pipecorn's account. By name, it must BE the company asked (namesake guard)."""
    if not isinstance(res, dict) or not res.get("found"):
        return None
    name = res.get("company_name")
    domain = _domain(res.get("domain") or res.get("website"))
    if not inputs.get("domain") and not _same_company(inputs, name, domain):
        return None
    hq = res.get("headquarters") or {}
    return _clean({
        "name": name,
        "domain": domain or inputs.get("domain"),
        "industry": res.get("industry"),
        "employees": res.get("employee_count"),
        # A size BAND (`201-500`), as Pipecorn gives it.
        "employees_band": res.get("employee_range"),
        "city": hq.get("city") if isinstance(hq, dict) else None,
        "country": hq.get("country") if isinstance(hq, dict) else None,
        "linkedin_url": res.get("linkedin_url"),
        "type": res.get("company_type"),
    }, ("name",))


def _theirstack_company(res, inputs):
    for rec in (_get(res, "data") or [])[:3]:
        if isinstance(rec, dict) and _same_company(inputs, rec.get("name"),
                                                   rec.get("domain")):
            return _clean({
                "name": rec.get("name"), "domain": _domain(rec.get("domain")),
                "employees": rec.get("employee_count"), "industry": rec.get("industry"),
                "linkedin_url": rec.get("linkedin_url"),
                "technologies": (rec.get("technology_names") or [])[:20] or None,
            }, ("name",))
    return None


def _cognism_company(res, inputs):
    for rec in (_get(res, "results") or [])[:3]:
        if not isinstance(rec, dict):
            continue
        name = _find_key(rec, ("name", "accountName", "companyName"), 2)
        domain = _find_key(rec, ("domain", "website", "websiteUrl"), 2)
        if not _same_company(inputs, name, domain):
            continue
        return _clean({
            "name": name, "domain": _domain(str(domain or "")),
            "industry": _find_key(rec, ("industry", "industries"), 2),
            "employees": _find_key(rec, ("size", "employees", "employeeCount",
                                         "headcount"), 2),
            "revenue": _find_key(rec, ("revenue", "annualRevenue"), 2),
            "country": _find_key(rec, ("country", "countryCode"), 3),
            "city": _find_key(rec, ("city",), 3),
            "linkedin_url": _find_key(rec, ("linkedinUrl", "linkedin_url", "linkedin"), 2),
        }, ("name",))
    return None


# The LinkedIn company page — two Apify actors, learned on a real list (a trade-show
# exhibitor list, 2026-10), where they recovered the pages the data vendors did not have.

def _site_linkedin(res, inputs):
    """The LinkedIn links the company's OWN website carries (`linkedIns`). A company
    page wins; a big group's site often links the PARENT's page — the scrape cannot
    tell, so the result says where it came from (`linkedin_source`)."""
    for item in _apify_items(res):
        if not isinstance(item, dict):
            continue
        links = item.get("linkedIns") or item.get("linkedins") or []
        pages = [p for p in (_company_page(u) for u in links) if p]
        if pages:
            return {"linkedin_url": pages[0], "linkedin_source": "website"}
    return None


def _search_linkedin(res, inputs):
    """A LinkedIn company search by NAME returns its top hit — usually a namesake on a
    short name (a restaurant, a dev shop elsewhere): kept only when its website is the
    company's domain, or, without a domain, when its name is the name asked."""
    for item in _apify_items(res)[:3]:
        if not isinstance(item, dict):
            continue
        url = _company_page(_pick(item, "linkedinUrl", "url", "companyUrl", "link"))
        if not url:
            continue
        website = _domain(_pick(item, "website", "websiteUrl", "companyWebsite"))
        name = _pick(item, "name", "companyName")
        if inputs.get("domain"):
            if website != inputs["domain"]:
                continue
        elif not _same_company(inputs, name, None):
            continue
        return _clean({"linkedin_url": url, "linkedin_source": "search", "name": name,
                       "domain": website,
                       "employees": _pick(item, "employeeCount", "staffCount"),
                       "industry": _pick(item, "industry", "industries[0]")},
                      ("linkedin_url",))
    return None


def _one_row(key: str, label: str, cost: str, *variants: Step, explains: str,
             fills: str = "") -> Step:
    """One row of the table made of several ways in (`Step.variants`): what a screen
    shows and a setting names once."""
    groups: list[tuple[str, ...]] = []
    for v in variants:
        groups += [g for g in v.requires if g not in groups]
    first = variants[0]
    return Step(key, first.tool, tuple(groups), first.build, first.extract, cost,
                free=all(v.free for v in variants),
                keyless=all(v.keyless for v in variants), label=label, explains=explains,
                fills=fills, variants=variants)


COMPANY_STEPS: tuple[Step, ...] = (
    _one_row(
        "fr", "French registry", "free (French open data)",
        Step("fr_siren", "fr_get", (("siren",),),
             lambda i: {"siren": i["siren"]}, _fr_get_company, "free (French open data)",
             free=True, keyless=True),
        Step("fr_name", "fr_search", (("company_name", "fr"),),
             lambda i: {"query": i["company_name"], "per_page": 5},
             _fr_search_company, "free (French open data)",
             needs="company_name, and a French company (country omitted or FR)",
             free=True, keyless=True),
        explains=(
            "The French company registry — legal name, SIREN, address, NAF code, size "
            "band; free, France only."),
    ),
    # Ahead of every paid record: it bills no credits (one call of the key's quota).
    Step("lemlist", "lemlist_database", (("domain",),),
         _lemlist_company_args, _lemlist_company,
         "free search (no lemlist credits; one call of the key's search quota)",
         label="lemlist database", quota_only=True,
         explains=(
             "lemlist's shared company database — headcount, size band, industry, "
             "headquarters, founding year, LinkedIn page; spends no credits.")),
    Step("aiark", "linkedin_aiark_search", (("domain",), ("company_name",)),
         _aiark_company_args, _aiark_company, "AI Ark credits per company returned",
         max_units=3,
         explains=(
             "LinkedIn-sourced company profile — industry, headcount, revenue range, "
             "phone.")),
    # The caller's own Pipecorn key only: a synchronous lookup is outside the units
    # budget, the shared key does not serve it (`tools/pipecorn.py`).
    Step("pipecorn", "pipecorn", (("domain",), ("company_name",)),
         lambda i: ({"op": "company", "domain": i["domain"]} if i.get("domain")
                    else {"op": "company", "company_name": i["company_name"]}),
         _pipecorn_company,
         "1 Pipecorn credit per company found, nothing on a miss (own key only)",
         asks="company", own_key_only=True, bounded_by_deadline=True,
         explains=(
             "Pipecorn's company record — industry, headcount and its growth, size band, "
             "headquarters, LinkedIn page. Your own Pipecorn key only.")),
    Step("apollo", "apollo_enrich_organization", (("domain",),),
         lambda i: {"domain": i["domain"]}, _apollo_company, "1 Apollo credit",
         explains=(
             "Apollo's company record — headcount, revenue, industry, LinkedIn page; needs "
             "the domain.")),
    Step("theirstack", "theirstack_companies_search", (("domain",),),
         # Billed PER COMPANY returned: the domain names one — never ask 3.
         lambda i: {"extra": {"company_domain_or": [i["domain"]]}, "limit": 1},
         _theirstack_company, "~3 TheirStack credits per company",
         explains=(
             "Company record built from its job ads — size, industry, and the technologies "
             "the ads name.")),
    Step("cognism", "cognism_enrich_account", (("domain",), ("company_name",)),
         lambda i: {k: v for k, v in {"name": i.get("company_name"),
                                      "domain": i.get("domain")}.items() if v},
         _cognism_company, "Cognism credits (per its contract)",
         explains="Cognism's company data — billed per your Cognism contract."),
    # Completion: the LinkedIn page, when no source above returned it (the French
    # registry has none) — lemlist's database first (no credits), then the company's own
    # website, then a search on its name. lemlist is its own row: a row's ways in share
    # ONE key mode, and lemlist's is not Apify's.
    Step("lemlist_linkedin", "lemlist_database", (("domain",),),
         _lemlist_company_args, _lemlist_company,
         "free search (no lemlist credits; one call of the key's search quota)",
         label="LinkedIn page — lemlist database", quota_only=True, fills="linkedin_url",
         same_as="lemlist",
         explains=(
             "Finds the LinkedIn page in lemlist's company database when no source gave "
             "it — no credits, before any Apify run.")),
    _one_row(
        "linkedin", "LinkedIn page", "a few cents of Apify per try",
        Step("linkedin_site", "apify_run_sync", (("domain",),),
             lambda i: {"actor_id": "vdrmota/contact-info-scraper",
                        "run_input": {"startUrls": [{"url": f"https://www.{i['domain']}"}],
                                      "maxDepth": 1, "maxRequestsPerStartUrl": 4,
                                      "sameDomain": True},
                        # ⚠️ No `max_items`: on this pay-per-event actor Apify derives
                        # a charge ceiling from it, under its $0.50 minimum — refused
                        # (measured 19/10/2026). Bounded by its 4 pages, depth 1, and time.
                        "timeout_secs": _APIFY_TIMEOUT_S},
             _site_linkedin, "a few cents of Apify per site"),
        Step("linkedin_search", "apify_run_sync", (("company_name",),),
             lambda i: {"actor_id": "harvestapi/linkedin-company",
                        "run_input": {"searches": [i["company_name"]]},
                        "max_items": 3, "timeout_secs": _APIFY_TIMEOUT_S},
             _search_linkedin, "a few cents of Apify per search"),
        fills="linkedin_url",
        explains=(
            "Finds the LinkedIn page when no source gave it — from the company's own "
            "website, else a search checked against the domain."),
    ),
)
