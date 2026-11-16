"""The tech-stack cascade (`waterfall_tech`): lemlist's company record first (no credits),
then what a company's website runs (DataForSEO's index, then a live Apify crawl), what the data vendors' company records list (Apollo, AI
Ark), then what its job postings name (TheirStack) — and, with `technologies`, a source
counts only when it detects one of them."""
from __future__ import annotations

import re
from typing import Optional

from .etape import Step
from .fournisseurs import (_APIFY_TIMEOUT_S, _apify_items, _lemlist_company_args,
                           _lemlist_company_rec)
from .valeurs import _domain, _get, _norm_name, _pick, _same_company

# Two kinds of source, measured on one domain (20/10/2026): what the WEBSITE runs (CMS,
# front framework, hosting, analytics — DataForSEO's index, a live crawl) — 6-7 names;
# and what the JOB POSTINGS name (CRM, ERP, data stack, internal tools — TheirStack) —
# ~100 slugs, but dearer and thin on small companies. Between them, the data vendors'
# company records (Apollo, AI Ark — measured the same day: ~100 categorized names each,
# website AND business tools: CRM, data stack, payments). Cheapest first, as everywhere.


def _tech_key(name) -> str:
    """`Next.js`, `next-js`, `nextjs` → one key: the website sources give display names,
    TheirStack gives slugs (`google-cloud-platform`, `spendesk1`)."""
    return re.sub(r"\d+$", "", _norm_name(str(name or "")).replace(" ", ""))


def _tech_find(names: list, inputs: dict, source: str, by_category=None) -> Optional[dict]:
    """The find: the names, deduplicated. With `technologies` asked, a source counts
    only when it detects at least one of them — a stack without them is not an answer
    to « does it use X? », and the next source (which sees other things) is tried."""
    # One entry per technology, whatever its case or punctuation (AI Ark lists both
    # « google analytics » and « Google Analytics »): the first spelling wins.
    seen: dict[str, str] = {}
    for n in names:
        if n and _tech_key(n) not in seen:
            seen[_tech_key(n)] = str(n)
    names = list(seen.values())
    if not names:
        return None
    found = {"technologies": names, "count": len(names), "source": source}
    if by_category:
        found["by_category"] = by_category
    wanted = inputs.get("wanted")
    if wanted:
        have = {_tech_key(n) for n in names}
        matched = [w for w in wanted if _tech_key(w) in have]
        if not matched:
            return None
        found["matched"] = matched
    return found


def _pipecorn_tech(res, inputs):
    """`technologies` (names), else the names of `technologies_with_confidence`."""
    if not isinstance(res, dict):
        return None
    names = res.get("technologies") or [
        t.get("name") for t in res.get("technologies_with_confidence") or []
        if isinstance(t, dict)]
    return _tech_find(list(names), inputs, "vendor_database")


def _dataforseo_tech(res, inputs):
    """`dataforseo(op="domain_technologies")` answers DataForSEO's envelope: the site's
    record is `tasks[0].result[0]`, its `technologies` `{group: {category: [names]}}` —
    flattened by category."""
    groups = _get(res, "tasks[0].result[0].technologies")
    if not isinstance(groups, dict):
        return None
    by_category: dict[str, list] = {}
    for cats in groups.values():
        for cat, names in (cats or {}).items() if isinstance(cats, dict) else ():
            by_category.setdefault(cat, []).extend(n for n in names or () if n)
    return _tech_find([n for ns in by_category.values() for n in ns], inputs, "website",
                      by_category)


def _website_tech(res, inputs):
    for item in _apify_items(res)[:1]:
        if not isinstance(item, dict) or item.get("error"):
            continue
        names = [t.get("name") for t in item.get("technologies") or ()
                 if isinstance(t, dict)]
        return _tech_find(names, inputs, "website", item.get("byCategory") or None)
    return None


def _by_category(records) -> Optional[dict]:
    """`[{name, category?}]` → `{category: [names]}` (records without one left out)."""
    out: dict[str, list] = {}
    for r in records or ():
        if isinstance(r, dict) and r.get("name") and r.get("category"):
            out.setdefault(str(r["category"]), []).append(r["name"])
    return out or None


def _apollo_tech(res, inputs):
    """Apollo's company record: `technology_names`, and `current_technologies`
    (`{uid, name, category}`) for the categories."""
    org = _get(res, "organization")
    if not isinstance(org, dict):
        return None
    if inputs.get("domain") and _domain(org.get("primary_domain")) not in (None, inputs["domain"]):
        return None
    current = org.get("current_technologies") or []
    names = org.get("technology_names") or [t.get("name") for t in current
                                            if isinstance(t, dict)]
    return _tech_find(names, inputs, "vendor_database", _by_category(current))


def _aiark_tech(res, inputs):
    """AI Ark's company record (`technologies`: `{name, category?}`) — website detections
    and business tools mixed, some names lower-cased."""
    for rec in (_get(res, "content") or [])[:1]:
        if not isinstance(rec, dict):
            continue
        domain = _domain(_pick(rec, "link.domain", "link.website"))
        if inputs.get("domain") and domain and domain != inputs["domain"]:
            continue
        techs = rec.get("technologies") or []
        return _tech_find([t.get("name") for t in techs if isinstance(t, dict)], inputs,
                          "vendor_database", _by_category(techs))
    return None


#: lemlist scores each detection CONFIRMED, LIKELY or POTENTIAL: a POTENTIAL one is not an
#: answer to « does it use X? ».
_LEMLIST_SURE = ("CONFIRMED", "LIKELY")


def _lemlist_tech(res, inputs):
    """lemlist's company record (`technologies`: `{name, scoring, first_seen_at}`) —
    website detections and business tools mixed (~200 names on a 300-person company,
    measured), kept when CONFIRMED or LIKELY."""
    rec = _lemlist_company_rec(res, inputs)
    if rec is None:
        return None
    names = [t.get("name") for t in rec.get("technologies") or ()
             if isinstance(t, dict) and t.get("scoring") in _LEMLIST_SURE]
    return _tech_find(names, inputs, "vendor_database")


def _theirstack_tech(res, inputs):
    for rec in (_get(res, "data") or [])[:3]:
        if isinstance(rec, dict) and _same_company(inputs, rec.get("name"),
                                                   rec.get("domain")):
            return _tech_find(rec.get("technology_names") or [], inputs, "job_postings")
    return None


def _theirstack_tech_args(inputs: dict) -> dict:
    # Billed PER COMPANY returned: the domain names one; a name may need a namesake
    # stepped over (3).
    if inputs.get("domain"):
        return {"extra": {"company_domain_or": [inputs["domain"]]}, "limit": 1}
    return {"extra": {"company_name_case_insensitive_or": [inputs["company_name"]]},
            "limit": 3}


TECH_STEPS: tuple[Step, ...] = (
    # First: a vendor record that bills no credits (one call of the key's quota).
    Step("lemlist", "lemlist_database", (("domain",),),
         _lemlist_company_args, _lemlist_tech,
         "free search (no lemlist credits; one call of the key's search quota)",
         label="lemlist database", quota_only=True,
         explains=(
             "lemlist's company record — website and business tools with a confidence "
             "(confirmed and likely kept); spends no credits.")),
    Step("dataforseo", "dataforseo", (("domain",),),
         lambda i: {"op": "domain_technologies", "target": i["domain"]},
         _dataforseo_tech, "~$0.01-0.02 per domain (DataForSEO)",
         label="Website — DataForSEO index",
         explains=(
             "What the website runs — CMS, framework, hosting, analytics, e-commerce — "
             "from DataForSEO's index of the web.")),
    # The live crawl — plain HTTP, no browser, billed per site analysed (a site that
    # fails or times out is free): a site DataForSEO has not indexed (small companies),
    # or a stack changed since its last visit. One row per provider, each with its own
    # logo: a screen says WHO looks.
    Step("apify", "apify_run_sync", (("domain",),),
         lambda i: {"actor_id": "kfirs/tech-stack-detector",
                    "run_input": {"urls": [i["domain"]], "minConfidence": 50,
                                  "includeDetails": False, "timeoutSecs": 20},
                    # ⚠️ No `max_items`: pay-per-event, Apify would derive a ceiling
                    # under its $0.50 minimum (see the LinkedIn page). One URL = one
                    # billed site at most.
                    "timeout_secs": _APIFY_TIMEOUT_S},
         _website_tech, "~$0.01 of Apify per site", label="Website — Apify live crawl",
         explains=(
             "The same, by crawling the site live with Apify — for a site DataForSEO has "
             "not indexed, or a stack that changed since.")),
    Step("apollo", "apollo_enrich_organization", (("domain",),),
         lambda i: {"domain": i["domain"]}, _apollo_tech, "1 Apollo credit",
         label="Apollo company record", max_units=1,
         explains=(
             "Apollo's company record — around a hundred tools with their categories, "
             "website and business software alike (CRM, data stack, payments).")),
    # `full` + `fields`: the default view drops `technologies`; one record, by domain.
    Step("aiark", "linkedin_aiark_search", (("domain",),),
         lambda i: {"op": "companies", "size": 1, "full": True,
                    "fields": ["summary", "link", "technologies"],
                    "account": {"domain": {"any": {"include": [i["domain"]]}}}},
         _aiark_tech, "AI Ark credits for one company", label="AI Ark company record",
         max_units=1,
         explains=(
             "AI Ark's company record — website detections and business tools mixed, with "
             "categories; a second vendor view.")),
    Step("theirstack", "theirstack_companies_search", (("domain",), ("company_name",)),
         _theirstack_tech_args, _theirstack_tech, "~3 TheirStack credits per company",
         label="Job postings (TheirStack)", max_units=3,
         explains=(
             "What the job ads name — CRM, ERP, data stack, internal tools the website "
             "never shows; thin on small companies.")),
    # The caller's own Pipecorn key only: a synchronous lookup is outside the units
    # budget, the shared key does not serve it (`tools/pipecorn.py`). Billed per
    # request, found or not: last, since it bills even when it finds nothing.
    Step("pipecorn", "pipecorn", (("domain",),),
         lambda i: {"op": "stack", "domain": i["domain"]},
         _pipecorn_tech, "3 Pipecorn credits per company, found or not (own key only)",
         label="Pipecorn stack", asks="technologies", max_units=3, own_key_only=True,
         bounded_by_deadline=True,
         explains=(
             "Pipecorn's technology record for the company — website and business tools, "
             "with a confidence. Your own Pipecorn key only.")),
)
