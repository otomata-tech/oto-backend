"""Apify — the dedicated actor tools (`tools/apify_actors.py`).

Locks: each tool is served under the `apify` namespace with a description, runs ITS
pinned actor with the input that actor's schema expects, always carries a cost
ceiling, and bills its run like `apify_run_sync` — once finished, under its own name.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import apify_actors as A

RUN = "HG7ML7M8z78YcAPEB"   # the SHAPE of an Apify run id, made up

# tool → (kwargs, actor, expected run_input, expected max_items)
CAS = {
    "apify_linkedin_profiles": (
        {"urls": ["https://www.linkedin.com/in/a", "b"]},
        A.LINKEDIN_PROFILES,
        {"queries": ["https://www.linkedin.com/in/a", "b"],
         "profileScraperMode": "Profile details no email ($4 per 1k)"},
        2),
    "apify_linkedin_company_employees": (
        {"companies": ["https://www.linkedin.com/company/acme"], "job_titles": ["CEO"],
         "max_items": 10},
        A.LINKEDIN_EMPLOYEES,
        {"companies": ["https://www.linkedin.com/company/acme"], "jobTitles": ["CEO"],
         "profileScraperMode": "Short ($4 per 1k)", "maxItems": 10},
        10),
    "apify_linkedin_companies": (
        {"names": ["Acme", "Globex"]},
        A.LINKEDIN_COMPANIES, {"searches": ["Acme", "Globex"]}, 2),
    "apify_linkedin_posts": (
        {"queries": ["revops"], "posted_limit": "week", "max_posts": 5},
        A.LINKEDIN_POSTS,
        {"searchQueries": ["revops"], "postedLimit": "week", "maxPosts": 5}, 5),
    "apify_linkedin_jobs": (
        {"keywords": "head of growth", "location": "France", "max_items": 30},
        A.LINKEDIN_JOBS,
        {"keywords": "head of growth", "location": "France", "scrapeCompany": True,
         "limitPerSource": 30},
        30),
    "apify_indeed_jobs": (
        {"title": "sales manager", "country": "FR", "max_items": 15},
        A.INDEED_JOBS, {"title": "sales manager", "country": "fr", "limit": 15}, 15),
    "apify_welcome_to_the_jungle_jobs": (
        {"query": "business developer", "country_code": "FR", "max_items": 20},
        A.WTTJ_JOBS,
        {"action": "get-jobs", "query": "business developer", "countryCode": "FR",
         "maxItems": 20},
        20),
    "apify_website_contacts": (
        {"urls": ["https://www.example.com"]},
        A.WEBSITE_CONTACTS,
        {"startUrls": [{"url": "https://www.example.com"}], "maxDepth": 1,
         "maxRequestsPerStartUrl": 4, "sameDomain": True, "mergeContacts": True,
         "proxyConfig": {"useApifyProxy": True}},
        1),
    "apify_website_crawl": (
        {"urls": ["https://example.com/docs"], "max_pages": 5},
        A.WEBSITE_CRAWL,
        {"startUrls": [{"url": "https://example.com/docs"}], "maxCrawlPages": 5,
         "maxResults": 5, "proxyConfiguration": {"useApifyProxy": True}},
        5),
    "apify_google_maps_places": (
        {"search_terms": ["bakery", "pastry"], "location": "Marseille, France",
         "max_places": 10},
        A.GOOGLE_MAPS_PLACES,
        {"searchStringsArray": ["bakery", "pastry"], "locationQuery": "Marseille, France",
         "maxCrawledPlacesPerSearch": 10, "language": "en"},
        20),
    "apify_google_maps_reviews": (
        {"place_ids": ["ChIJ-made-up"], "max_reviews": 30, "since": "3 months"},
        A.GOOGLE_MAPS_REVIEWS,
        {"placeIds": ["ChIJ-made-up"], "maxReviews": 30, "reviewsSort": "newest",
         "reviewsStartDate": "3 months", "language": "en"},
        30),
}


def _tool(name):
    from fastmcp import FastMCP

    m = FastMCP("t")
    A.register(m)
    return asyncio.run(m.get_tool(name))


def _lancer(name, kwargs):
    """Calls the tool on a run that finishes at once; returns
    `(output, client mock, traces)`."""
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
            patch("oto.tools.apify.client.ApifyClient") as cls, \
            patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace:
        inst = cls.return_value
        inst.run.return_value = {"data": {"id": RUN, "status": "SUCCEEDED",
                                          "usageTotalUsd": 0.0421,
                                          "defaultDatasetId": "ds1"}}
        inst.dataset_items.return_value = [{"x": 1}]
        out = _tool(name).fn(**kwargs)
    return out, inst, [c.kwargs for c in trace.call_args_list]


def test_every_dedicated_tool_is_served_under_apify_with_a_description():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    servis = {t.name: t for t in asyncio.run(m._list_tools())}
    for name in CAS:
        assert name in servis, name
        assert namespace_of(name) == "apify"
        assert (servis[name].description or "").strip(), f"{name} has no description"


def test_the_cases_cover_every_dedicated_tool():
    from fastmcp import FastMCP

    m = FastMCP("t")
    A.register(m)
    assert {t.name for t in asyncio.run(m._list_tools())} == set(CAS)


@pytest.mark.parametrize("name", sorted(CAS))
def test_runs_its_actor_with_the_input_its_schema_expects(name):
    kwargs, actor, run_input, max_items = CAS[name]
    out, inst, _ = _lancer(name, kwargs)
    args, kw = inst.run.call_args
    assert args == (actor,)
    assert kw["run_input"] == run_input
    assert kw["max_items"] == max_items
    assert out == [{"x": 1}]


@pytest.mark.parametrize("name", sorted(CAS))
def test_always_carries_a_cost_ceiling(name):
    kwargs = CAS[name][0]
    _, inst, _ = _lancer(name, kwargs)
    assert inst.run.call_args.kwargs["max_total_charge_usd"] == 5.0
    _, inst, _ = _lancer(name, {**kwargs, "max_total_charge_usd": 12.5})
    assert inst.run.call_args.kwargs["max_total_charge_usd"] == 12.5


@pytest.mark.parametrize("name", sorted(CAS))
def test_bills_the_finished_run_once_like_run_sync(name):
    _, _, traces = _lancer(name, CAS[name][0])
    assert traces == [{"quantity": 42100, "apify_run_id": RUN}]


def test_fields_reach_the_dataset_read():
    _, inst, _ = _lancer("apify_linkedin_profiles",
                         {"urls": ["a"], "fields": ["firstName", "headline"]})
    assert inst.dataset_items.call_args.kwargs["fields"] == ["firstName", "headline"]


def test_email_search_switches_the_profile_mode():
    _, inst, _ = _lancer("apify_linkedin_profiles", {"urls": ["a"], "with_email": True})
    assert inst.run.call_args.kwargs["run_input"]["profileScraperMode"] == \
        "Profile details + email search ($10 per 1k)"


def test_reactions_lift_the_item_cap_but_not_the_dollar_ceiling():
    _, inst, _ = _lancer("apify_linkedin_posts",
                         {"author_urls": ["https://www.linkedin.com/in/a"],
                          "with_reactions": True})
    kw = inst.run.call_args.kwargs
    assert kw["max_items"] is None and kw["max_total_charge_usd"] == 5.0
    assert kw["run_input"]["scrapeReactions"] is True


@pytest.mark.parametrize("name,kwargs", [
    ("apify_linkedin_profiles", {"urls": []}),
    ("apify_linkedin_company_employees", {"companies": []}),
    ("apify_linkedin_companies", {}),
    ("apify_linkedin_posts", {}),
    ("apify_linkedin_jobs", {"location": "France"}),
    ("apify_welcome_to_the_jungle_jobs", {"city": "Paris"}),
    ("apify_website_contacts", {"urls": []}),
    ("apify_website_crawl", {"urls": []}),
    ("apify_google_maps_places", {"search_terms": [], "location": "Paris"}),
    ("apify_google_maps_reviews", {}),
])
def test_a_run_with_nothing_to_scrape_is_refused_before_it_starts(name, kwargs):
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
            patch("oto.tools.apify.client.ApifyClient") as cls:
        with pytest.raises(McpError):
            _tool(name).fn(**kwargs)
        cls.return_value.run.assert_not_called()


def test_a_failed_run_is_answered_with_its_run_not_thrown():
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
            patch("oto.tools.apify.client.ApifyClient") as cls, \
            patch("oto_mcp.tools.apify.session_org.note_call_trace"):
        inst = cls.return_value
        inst.run.return_value = {"data": {"id": RUN, "status": "FAILED",
                                          "usageTotalUsd": 0.01,
                                          "defaultDatasetId": "ds1"}}
        inst.dataset_items.return_value = []
        out = _tool("apify_indeed_jobs").fn(title="x")
    assert out["run_id"] == RUN and out["status"] == "FAILED"
    assert "apify_run_status" in out["error"]


def test_website_contacts_caps_its_billed_results_at_one_row_per_site():
    _, inst, _ = _lancer("apify_website_contacts",
                         {"urls": ["https://a.example", "https://b.example",
                                   "https://c.example"]})
    assert inst.run.call_args.kwargs["max_items"] == 3


@pytest.mark.parametrize("name", sorted(CAS))
def test_the_ceiling_has_no_upper_limit(name):
    _, inst, _ = _lancer(name, {**CAS[name][0], "max_total_charge_usd": 250})
    assert inst.run.call_args.kwargs["max_total_charge_usd"] == 250


@pytest.mark.parametrize("usd", [0, -1])
@pytest.mark.parametrize("name", sorted(CAS))
def test_a_ceiling_at_or_below_zero_is_refused_by_name_before_the_run(name, usd):
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
            patch("oto.tools.apify.client.ApifyClient") as cls:
        with pytest.raises(McpError) as e:
            _tool(name).fn(**{**CAS[name][0], "max_total_charge_usd": usd})
        cls.return_value.run.assert_not_called()
    assert "max_total_charge_usd" in str(e.value)


@pytest.mark.parametrize("name", sorted(CAS))
def test_the_ceiling_is_declared_in_the_served_text(name):
    prop = _tool(name).parameters["properties"]["max_total_charge_usd"]
    assert prop["default"] == 5.0 and "raise it" in prop["description"], name


def test_no_platform_usage_figure_is_written_in_the_module_or_served():
    """The platform key's run counts are account data: never in a public docstring
    nor in what a tool serves."""
    import re
    from fastmcp import FastMCP

    m = FastMCP("t")
    A.register(m)
    textes = [A.__doc__ or ""] + [t.description or "" for t in asyncio.run(m._list_tools())]
    for texte in textes:
        assert "totalRuns" not in texte
        assert not re.search(r"\d[\d,.]*k?\s+runs\b|\bruns?\s+\d", texte), texte[:120]
        assert not re.search(r"\bmost used\b|\bwe run most\b", texte), texte[:120]
