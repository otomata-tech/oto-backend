"""DataForSEO connector — SEO data (keywords, domains, backlinks), pay-as-you-go.

Locks: the registry entry (two-field credential, byo + CLOSED paid platform
key), the MCP surface, the op dispatch to the oto-core client, the refusals
made BEFORE a paid call (missing location/language, unused argument, limit),
the admin path (both fields posted, read back), the billing trace (real USD cost →
micro-dollars), the shared key's balance kept private, and the trimmed view.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import dataforseo as dfs_tool

pytestmark = pytest.mark.exige_pin_oto_core

EXPECTED_TOOLS = {"dataforseo_keywords", "dataforseo_domain", "dataforseo_backlinks",
                  "dataforseo_account", "dataforseo_ai_visibility",
                  "dataforseo_web_mentions", "dataforseo_site_audit",
                  "dataforseo_technologies"}


def _register_all(m):
    from oto_mcp.tools import dataforseo_insights, dataforseo_site
    dfs_tool.register(m)
    dataforseo_insights.register(m)
    dataforseo_site.register(m)


def _task(result=None, cost=0.0103):
    return {"id": "t", "status_code": 20000, "cost": cost,
            "result": result if result is not None else [{"items": []}]}


@pytest.fixture()
def env():
    """A registered surface over a mocked client and a mocked credential."""
    from fastmcp import FastMCP

    state = SimpleNamespace(
        rc=SimpleNamespace(fields={"login": "me@example.com", "password": "pw"},
                           config={}, is_platform=False),
        traces=[], client=MagicMock())
    from oto.tools.dataforseo import DataForSeoClient
    for m in dir(DataForSeoClient):
        if not m.startswith("_"):
            getattr(state.client, m).return_value = _task()
    with patch("oto.tools.dataforseo.DataForSeoClient") as cls, \
            patch.object(dfs_tool.access, "resolve_credential",
                         side_effect=lambda *a, **k: state.rc), \
            patch.object(dfs_tool.session_org, "note_call_trace",
                         side_effect=lambda **v: state.traces.append(v)):
        cls.side_effect = lambda login, password: (
            state.__setattr__("creds", (login, password)) or state.client)
        m = FastMCP("t")
        _register_all(m)
        state.call = lambda name, **kw: asyncio.run(m.get_tool(name)).fn(**kw)
        yield state


# --- registry -----------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["dataforseo"]
    assert c.kind == "tools"
    assert c.secret_kind == "fields"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    # Paid key: posted closed, served to granted orgs only (`set_platform_key`).
    assert c.platform_key_open is False
    assert [(f.name, f.secret) for f in c.secret_fields] == [("login", False),
                                                            ("password", True)]
    assert c.modules == ("dataforseo", "dataforseo_insights", "dataforseo_site")
    assert c.category == "Marketing & SEO"
    assert c.publisher_name == "DataForSEO"


def test_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["dataforseo"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


def test_tools_register_under_namespace(env):
    from fastmcp import FastMCP
    m = FastMCP("t")
    _register_all(m)
    tools = {t.name: t for t in asyncio.run(m._list_tools())}
    assert set(tools) == EXPECTED_TOOLS
    assert all(namespace_of(n) == "dataforseo" for n in tools)
    assert all(t.description for t in tools.values())


# --- credential -----------------------------------------------------------------

def test_two_fields(env):
    env.call("dataforseo_backlinks", op="summary", target="example.com")
    assert env.creds == ("me@example.com", "pw")


@pytest.mark.parametrize("fields", [
    {"password": "me@example.com:pw"},
    {"login": "me@example.com:pw"},
    {"key": "me@example.com:pw"},
])
def test_a_single_login_colon_password_value_is_refused(env, fields):
    """One value for two fields is a credential posted wrong: refused, not split."""
    env.rc = SimpleNamespace(fields=fields, config={}, is_platform=True)
    with pytest.raises(McpError, match="login"):
        env.call("dataforseo_backlinks", op="summary", target="example.com")


def test_incomplete_credential_is_named(env):
    env.rc = SimpleNamespace(fields={"password": "pw"}, config={}, is_platform=False)
    with pytest.raises(McpError, match="login"):
        env.call("dataforseo_backlinks", op="summary", target="example.com")


# --- dispatch -------------------------------------------------------------------

def test_keywords_overview(env):
    env.call("dataforseo_keywords", op="overview", keywords=["crm"], location=2250,
             language="fr")
    env.client.keyword_overview.assert_called_once_with(
        ["crm"], options=None, location_code=2250, language_code="fr")


def test_location_name_and_language_name(env):
    env.call("dataforseo_keywords", op="ideas", keywords=["crm"], location="France",
             language="French", limit=5)
    kw = env.client.keyword_ideas.call_args.kwargs
    assert kw["location_name"] == "France" and kw["language_name"] == "French"
    assert kw["limit"] == 5


def test_list_ops_default_to_20_rows(env):
    env.call("dataforseo_domain", op="keywords", target="example.com")
    assert env.client.ranked_keywords.call_args.kwargs["limit"] == 20


def test_gap_defaults_to_shared_keywords(env):
    env.call("dataforseo_domain", op="gap", target="a.com", target2="b.com",
             location=2840, language="en")
    args = env.client.domain_intersection.call_args
    assert args.args == ("a.com", "b.com") and args.kwargs["intersections"] is True
    env.call("dataforseo_domain", op="gap", target="a.com", target2="b.com",
             location=2840, language="en", shared_only=False)
    assert env.client.domain_intersection.call_args.kwargs["intersections"] is False


@pytest.mark.parametrize("tool,kw,method", [
    ("dataforseo_keywords", dict(op="volume", keywords=["a"]), "google_ads_search_volume"),
    ("dataforseo_keywords", dict(op="intent", keywords=["a"]), "search_intent"),
    ("dataforseo_keywords", dict(op="suggestions", keyword="a"), "keyword_suggestions"),
    ("dataforseo_keywords", dict(op="related", keyword="a", location=2840, language="en"),
     "related_keywords"),
    ("dataforseo_keywords", dict(op="difficulty", keywords=["a"], location=2840,
                                 language="en"), "bulk_keyword_difficulty"),
    ("dataforseo_domain", dict(op="overview", target="a.com"), "domain_rank_overview"),
    ("dataforseo_domain", dict(op="pages", target="a.com"), "relevant_pages"),
    ("dataforseo_domain", dict(op="competitors", target="a.com", location=2840,
                               language="en"), "competitors_domain"),
    ("dataforseo_domain", dict(op="relevant_keywords", target="a.com", location=2840),
     "keywords_for_site"),
    ("dataforseo_domain", dict(op="traffic", targets=["a.com"]), "bulk_traffic_estimation"),
    ("dataforseo_backlinks", dict(op="list", target="a.com"), "backlinks_list"),
    ("dataforseo_backlinks", dict(op="referring_domains", target="a.com"),
     "referring_domains"),
    ("dataforseo_backlinks", dict(op="anchors", target="a.com"), "anchors"),
    ("dataforseo_backlinks", dict(op="ranks", targets=["a.com"]), "bulk_ranks"),
])
def test_each_op_reaches_its_method(env, tool, kw, method):
    env.call(tool, **kw)
    assert getattr(env.client, method).call_count == 1


# --- refusals before a paid call ----------------------------------------------------

@pytest.mark.parametrize("tool,kw,match", [
    ("dataforseo_keywords", dict(op="overview", keywords=["a"], language="en"), "location"),
    ("dataforseo_keywords", dict(op="overview", keywords=["a"], location=2840), "language"),
    ("dataforseo_domain", dict(op="gap", target="a.com", target2="b.com"), "location"),
    ("dataforseo_keywords", dict(op="nope"), "op must be"),
    ("dataforseo_keywords", dict(op="volume", keywords=["a"], depth=2), "depth"),
    ("dataforseo_keywords", dict(op="suggestions", keywords=["a"], keyword="a"), "keywords"),
    ("dataforseo_domain", dict(op="overview", target="a.com", limit=5), "limit"),
    ("dataforseo_domain", dict(op="keywords"), "target"),
    ("dataforseo_domain", dict(op="keywords", target="a.com", limit=5000), "limit"),
    ("dataforseo_backlinks", dict(op="summary", target="a.com", targets=["b"]), "targets"),
    ("dataforseo_backlinks", dict(op="anchors", target="a.com", mode="as_is"), "mode"),
])
def test_refused_without_spending(env, tool, kw, match):
    with pytest.raises(McpError, match=match):
        env.call(tool, **kw)
    assert env.traces == []


# --- billing ----------------------------------------------------------------------

def test_cost_is_traced_in_microdollars(env):
    env.client.backlinks_summary.return_value = _task(result=[{"rank": 3}], cost=0.02003)
    out = env.call("dataforseo_backlinks", op="summary", target="a.com")
    assert env.traces == [{"quantity": 20030}]
    assert out["cost_usd"] == 0.02003


def test_float_noise_does_not_overbill():
    assert dfs_tool._microdollars(3 * 1e-05) == 30
    assert dfs_tool._microdollars(None) == 0


def test_upstream_refusal_bills_nothing(env):
    from oto.tools.common.errors import UpstreamHTTPError
    env.client.backlinks_summary.side_effect = UpstreamHTTPError(
        402, {"status_code": 40200, "status_message": "Payment Required."},
        service="dataforseo")
    env.rc = SimpleNamespace(fields={"login": "a@b.c", "password": "pw"}, config={},
                             is_platform=True)
    with pytest.raises(McpError, match="shared DataForSEO balance is empty"):
        env.call("dataforseo_backlinks", op="summary", target="a.com")
    assert env.traces == []


# --- account ----------------------------------------------------------------------

def test_unverified_account_says_so(env):
    from oto.tools.common.errors import UpstreamHTTPError
    env.client.backlinks_summary.side_effect = UpstreamHTTPError(
        403, {"status_code": 40104,
              "status_message": "Please verify your account before using the API."},
        service="dataforseo")
    with pytest.raises(McpError, match="verify your account") as e:
        env.call("dataforseo_backlinks", op="summary", target="a.com")
    assert "login/password" not in str(e.value)


def test_balance_of_own_key(env):
    env.client.user_data.return_value = _task(
        result=[{"login": "me@example.com", "money": {"balance": 41.5, "total": 50}}], cost=0)
    assert env.call("dataforseo_account", op="balance") == {
        "login": "me@example.com", "balance_usd": 41.5, "total_paid_usd": 50}


def test_balance_refused_on_the_shared_key(env):
    env.rc = SimpleNamespace(fields={"login": "a@b.c", "password": "pw"}, config={},
                             is_platform=True)
    with pytest.raises(McpError, match="shared key"):
        env.call("dataforseo_account", op="balance")
    env.client.user_data.assert_not_called()


def test_locations_filtered(env):
    env.client.labs_locations_and_languages.return_value = _task(result=[
        {"location_code": 2250, "location_name": "France", "location_type": "Country",
         "country_iso_code": "FR",
         "available_languages": [{"language_name": "French", "language_code": "fr",
                                  "keywords": 1}]},
        {"location_code": 2840, "location_name": "United States"}], cost=0)
    out = env.call("dataforseo_account", op="locations", query="fra")
    assert out["count"] == 1
    assert out["items"][0]["languages"] == [{"language_name": "French",
                                             "language_code": "fr"}]


# --- view -------------------------------------------------------------------------

def test_default_view_trims_and_full_does_not(env):
    raw = [{"se_type": "google", "total_count": 2, "items_count": 1, "offset_token": None,
            "items": [{"keyword": "crm", "keyword_info": {
                "search_volume": 100, "monthly_searches": [{"month": 1}], "cpc": None}}]}]
    env.client.ranked_keywords.return_value = _task(result=raw, cost=0.0102)
    out = env.call("dataforseo_domain", op="keywords", target="a.com")
    assert out == {"cost_usd": 0.0102, "total_count": 2, "items_count": 1,
                   "items": [{"keyword": "crm", "keyword_info": {"search_volume": 100}}]}
    out = env.call("dataforseo_domain", op="keywords", target="a.com", full=True)
    assert out == {"cost_usd": 0.0102, "result": raw}


def test_volume_rows_always_in_items(env):
    env.client.google_ads_search_volume.return_value = _task(
        result=[{"keyword": "crm", "search_volume": 90500, "monthly_searches": []}])
    out = env.call("dataforseo_keywords", op="volume", keywords=["crm"])
    assert out["items"] == [{"keyword": "crm", "search_volume": 90500}]


def test_verify_probe_uses_the_free_call():
    with patch("oto.tools.dataforseo.DataForSeoClient") as cls:
        dfs_tool._verify({"password": "pw"}, {"login": "me@example.com"})
    cls.assert_called_once_with(login="me@example.com", password="pw")
    cls.return_value.user_data.assert_called_once_with()


# --- added families -----------------------------------------------------------------

@pytest.mark.parametrize("tool,kw,method", [
    ("dataforseo_keywords", dict(op="trends", keywords=["a", "b"], time_range="past_5_years"),
     "google_trends_explore"),
    ("dataforseo_keywords", dict(op="ai_volume", keywords=["a"], location=2840, language="en"),
     "ai_keyword_search_volume"),
    ("dataforseo_domain", dict(op="history", target="a.com", location=2840, language="en",
                               date_from="2026-01-01"), "historical_rank_overview"),
    ("dataforseo_domain", dict(op="serp_competitors", keywords=["crm"], location=2840,
                               language="en"), "serp_competitors"),
    ("dataforseo_domain", dict(op="subdomains", target="a.com"), "subdomains"),
    ("dataforseo_backlinks", dict(op="top_pages", target="a.com"), "backlinks_domain_pages"),
    ("dataforseo_backlinks", dict(op="competitors", target="a.com"), "backlinks_competitors"),
    ("dataforseo_backlinks", dict(op="history", target="a.com"), "backlinks_history"),
    ("dataforseo_ai_visibility", dict(op="metrics", domain="a.com"), "llm_mentions_metrics"),
    ("dataforseo_ai_visibility", dict(op="mentions", keyword="acme"), "llm_mentions_search"),
    ("dataforseo_ai_visibility", dict(op="top_domains", keyword="crm"),
     "llm_mentions_top_domains"),
    ("dataforseo_ai_visibility", dict(op="top_pages", keyword="crm"), "llm_mentions_top_pages"),
    ("dataforseo_ai_visibility", dict(op="history", domain="a.com"), "llm_mentions_history"),
    ("dataforseo_ai_visibility", dict(op="ai_mode", keyword="best crm", location=2840,
                                      language="en"), "google_ai_mode"),
    ("dataforseo_web_mentions", dict(op="summary", keyword="acme"), "content_summary"),
    ("dataforseo_web_mentions", dict(op="search", keyword="acme"), "content_search"),
    ("dataforseo_site_audit", dict(op="page", url="https://a.com"), "onpage_instant"),
    ("dataforseo_site_audit", dict(op="content", url="https://a.com"), "onpage_content"),
    ("dataforseo_site_audit", dict(op="crawl_status", task_id="t1"), "onpage_summary"),
    ("dataforseo_technologies", dict(op="domain", target="a.com"), "domain_technologies"),
    ("dataforseo_technologies", dict(op="domains_using", technologies=["Shopify"]),
     "domains_by_technology"),
    ("dataforseo_technologies", dict(op="summary", keywords=["bakery"]),
     "technologies_summary"),
])
def test_added_ops_reach_their_method(env, tool, kw, method):
    env.call(tool, **kw)
    assert getattr(env.client, method).call_count == 1


@pytest.mark.parametrize("tool,kw,match", [
    ("dataforseo_keywords", dict(op="trends", keywords=list("abcdef")), "at most 5"),
    ("dataforseo_keywords", dict(op="overview", keywords=["a"], location=1, language="en",
                                 time_range="past_5_years"), "time_range"),
    ("dataforseo_domain", dict(op="history", target="a.com"), "location"),
    ("dataforseo_domain", dict(op="keywords", target="a.com", keywords=["x"]), "keywords"),
    ("dataforseo_backlinks", dict(op="new_lost", target="a.com"), "date_from"),
    ("dataforseo_backlinks", dict(op="summary", target="a.com", group_range="month"),
     "group_range"),
    ("dataforseo_backlinks", dict(op="gap", target="a.com"), "target"),
    ("dataforseo_ai_visibility", dict(op="metrics"), "domain"),
    ("dataforseo_ai_visibility", dict(op="metrics", domain="a.com",
                                      targets=[{"domain": "b.com"}]), "not both"),
    ("dataforseo_ai_visibility", dict(op="ask", engine="claude", model="m"), "prompt"),
    ("dataforseo_ai_visibility", dict(op="ai_mode", keyword="x", language="en"), "location"),
    ("dataforseo_ai_visibility", dict(op="metrics", domain="a.com", prompt="x"), "prompt"),
    ("dataforseo_web_mentions", dict(op="search", keyword="a", limit=500), "limit"),
    ("dataforseo_site_audit", dict(op="lighthouse"), "url"),
    ("dataforseo_site_audit", dict(op="crawl_results", task_id="t1"), "report"),
    ("dataforseo_site_audit", dict(op="crawl_start", target="a.com", max_pages=50000),
     "max_pages"),
    ("dataforseo_site_audit", dict(op="page", url="https://a.com", task_id="t"), "task_id"),
    ("dataforseo_technologies", dict(op="domains_using"), "technologies"),
])
def test_added_refusals(env, tool, kw, match):
    with pytest.raises(McpError, match=match):
        env.call(tool, **kw)
    assert env.traces == []


def test_backlink_gap_and_new_lost_defaults(env):
    env.call("dataforseo_backlinks", op="gap", targets=["c1.com", "c2.com"],
             exclude_targets=["me.com"])
    gap = env.client.backlinks_intersection.call_args
    assert gap.args == (["c1.com", "c2.com"],)
    assert gap.kwargs["exclude_targets"] == ["me.com"] and gap.kwargs["limit"] == 20
    env.call("dataforseo_backlinks", op="new_lost", target="a.com", date_from="2026-08-01")
    kw = env.client.backlinks_new_lost.call_args.kwargs
    assert kw["date_from"] == "2026-08-01" and kw["date_to"] == dfs_tool.yesterday()


def test_mentions_targets_and_default_limit(env):
    env.call("dataforseo_ai_visibility", op="mentions", domain="a.com", keyword="acme",
             platform="google")
    args = env.client.llm_mentions_search.call_args
    assert args.args == ([{"domain": "a.com"}, {"keyword": "acme"}],)
    assert args.kwargs["limit"] == 5 and args.kwargs["platform"] == "google"


def test_ask_and_models(env):
    env.call("dataforseo_ai_visibility", op="ask", engine="perplexity", model="sonar",
             prompt="best crm in France?", web_search=True, country="FR")
    env.client.llm_response.assert_called_once_with(
        "perplexity", "best crm in France?", "sonar", web_search=True,
        web_search_country_iso_code="FR", options=None)
    env.client.llm_models.return_value = _task(
        result=[{"model_name": "sonar"}, {"model_name": "sonar-pro"}], cost=0)
    assert env.call("dataforseo_ai_visibility", op="models", engine="perplexity") == {
        "items": ["sonar", "sonar-pro"]}


def test_lighthouse_keeps_scores_and_failed_audits(env):
    env.client.lighthouse.return_value = _task(result=[{
        "finalUrl": "https://a.com/", "fetchTime": "t",
        "categories": {"seo": {"score": 0.92}, "performance": {"score": 0.41}},
        "audits": {"a": {"title": "A", "score": 1}, "b": {"title": "B", "score": 0.2},
                   "c": {"title": "C", "score": 0.5, "displayValue": "3 s"},
                   "d": {"title": "D", "score": None}}}], cost=0.005)
    out = env.call("dataforseo_site_audit", op="lighthouse", url="https://a.com")
    assert out["scores"] == {"seo": 92, "performance": 41}
    assert [a["id"] for a in out["failed_audits"]] == ["b", "c"]
    assert out["cost_usd"] == 0.005 and env.traces == [{"quantity": 5000}]


def test_crawl_start_returns_the_task_id(env):
    env.client.onpage_crawl_start.return_value = {"id": "t-9", "status_code": 20100,
                                                  "cost": 0.00075, "result": None}
    out = env.call("dataforseo_site_audit", op="crawl_start", target="a.com", max_pages=5)
    env.client.onpage_crawl_start.assert_called_once_with("a.com", 5, options=None)
    assert out["task_id"] == "t-9" and out["max_pages"] == 5
    assert env.traces == [{"quantity": 750}]


def test_broken_links_report_filters_for_you(env):
    env.call("dataforseo_site_audit", op="crawl_results", task_id="t1", report="broken_links")
    assert env.client.onpage_links.call_args.kwargs["filters"] == [["is_broken", "=", True]]


def test_free_read_traces_zero_not_null(env):
    env.client.onpage_summary.return_value = _task(result=[{"crawl_progress": "finished"}],
                                                   cost=0)
    env.call("dataforseo_site_audit", op="crawl_status", task_id="t1")
    assert env.traces == [{"quantity": 0}]


# --- the /admin platform-key path ----------------------------------------------------

def test_admin_posts_both_fields_and_the_tool_reads_them():
    """/admin (`/api/admin/platform-keys`) posts `fields` for DataForSEO; the stored
    secret reads back (`ResolvedCredential.fields` → `unpack_secret`) into the pair."""
    from oto_mcp import credentials_store as cs
    secret = cs.secret_from_input("dataforseo", None,
                                  {"login": "abc123", "password": "p:w"})
    assert dfs_tool.creds(cs.unpack_secret("dataforseo", secret)) == ("abc123", "p:w")


def test_admin_lone_password_is_refused_for_the_missing_login():
    from oto_mcp import credentials_store as cs
    with pytest.raises(cs.CredentialFieldsInvalid) as e:
        cs.secret_from_input("dataforseo", None, {"password": "pw"})
    assert e.value.code == "missing_credentials" and "API login" in e.value.message
