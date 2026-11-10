"""Connecteur Ahrefs — SEO/backlinks/keywords/rank-tracking/analytics (api.ahrefs.com).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection), la
doc how-to, la surface MCP (19 tools, chacun avec une description — régression
du piège f-string-docstring), la sonde « tester la connexion », la jointure
tool↔client oto-core pour les DEUX styles d'appel de ce module (direct
`client.method()` et dispatch dynamique `getattr(client, method_name)()` —
ce dernier hors de portée du garde-fou statique générique, recouvert ici à la
main), et le dispatch report/op (required manquant refusé, `extra` fusionné,
report inconnu refusé).
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import ahrefs

EXPECTED_TOOLS = {
    "ahrefs_site_explorer", "ahrefs_keywords_explorer", "ahrefs_site_audit",
    "ahrefs_rank_tracker", "ahrefs_serp_overview", "ahrefs_batch_analysis",
    "ahrefs_account", "ahrefs_project", "ahrefs_project_keywords",
    "ahrefs_project_competitors", "ahrefs_locations", "ahrefs_keyword_list",
    "ahrefs_brand_radar_report", "ahrefs_brand_radar_prompt", "ahrefs_brand_radar",
    "ahrefs_web_analytics", "ahrefs_gsc", "ahrefs_social", "ahrefs_public",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


def _registered_tools():
    """Enregistre le module seul (pas tout `register_all`) et rend {nom: Tool}."""
    from fastmcp import FastMCP

    m = FastMCP("t")
    ahrefs.register(m)
    return m, {t.name: t for t in asyncio.run(m._list_tools())}


def _fn(name):
    m, _ = _registered_tools()
    return asyncio.run(m.get_tool(name)).fn


def _fn_with_mock_client():
    """Enregistre le module avec `AhrefsClient` mocké, DANS le patch (sinon
    `register()`'s `from ... import AhrefsClient` capture la vraie classe avant
    que le patch ne s'applique — piège vécu, cf. test_theirstack.py)."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.ahrefs.client.AhrefsClient")
    cls = patcher.start()
    m = FastMCP("t")
    ahrefs.register(m)
    return m, cls, patcher


# --- registre -------------------------------------------------------------------

def test_ahrefs_is_keyed_byo_only_connector():
    c = providers.REGISTRY["ahrefs"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert "ahrefs" in providers.KEY_PROVIDERS
    assert c.category == "Marketing & SEO"
    assert c.publisher_name == "Ahrefs"
    assert c.label == "Ahrefs"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["ahrefs"] == "ahrefs.com"


def test_ahrefs_registered_among_keyed_connectors():
    """L'ordre des `keyed` est chargé (`status_for` en dépend) : on n'insère
    jamais au milieu. On vérifie l'APPARTENANCE, pas la position — deux
    connecteurs fusionnés le même jour ne peuvent pas être « le dernier »
    tous les deux (vécu le 21/08/2026 : main au rouge sur ce motif)."""
    keyed_names = [c.name for c in providers._REGISTRY_LIST if c.keyed]
    assert "ahrefs" in keyed_names


def test_ahrefs_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["ahrefs"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------------

def test_ahrefs_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "ahrefs" for t in all_tools if t.startswith("ahrefs_"))


def test_ahrefs_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    _registered_tools()  # register() appelle connector_verify.register("ahrefs", ...)
    assert connector_verify.supports("ahrefs")


# --- jointure tool ↔ client oto-core (garde version-skew) -------------------------
# `ahrefs.py` appelle le client de deux façons : direct (`client.foo()`, dans les ops
# Management/Social/Public — visible à la sonde générique AST) et dynamique
# (`getattr(client, method_name)(**kwargs)` dans `_call_report`, pour les 4 tools à
# axe `report=` — INVISIBLE à cette sonde, cf. `_DYNAMIC_DISPATCH_CLIENTS` dans
# `test_tools_client_methods_exist.py`). On recouvre le dispatch dynamique ici à la
# main : chaque entrée de chaque table de dispatch doit pointer une vraie méthode.

def test_dispatch_tables_point_to_real_client_methods():
    from oto.tools.ahrefs.client import AhrefsClient

    tables = [
        ahrefs._SITE_EXPLORER_REPORTS, ahrefs._KEYWORDS_EXPLORER_REPORTS,
        ahrefs._SITE_AUDIT_REPORTS, ahrefs._RANK_TRACKER_REPORTS,
        ahrefs._BRAND_RADAR_GET_REPORTS, ahrefs._BRAND_RADAR_POST_REPORTS,
    ]
    for table in tables:
        for report, (method_name, _required) in table.items():
            assert callable(getattr(AhrefsClient, method_name, None)), \
                f"report={report!r} → AhrefsClient.{method_name} manquant"


def test_client_exposes_methods_called_directly_by_tools():
    from oto.tools.ahrefs.client import AhrefsClient
    directly_called = (
        "subscription_info_limits_and_usage", "serp_overview", "batch_analysis",
        "list_projects", "create_project", "project_keywords", "add_project_keywords",
        "tag_project_keywords", "project_competitors", "add_project_competitors",
        "locations", "keyword_list_keywords", "add_keyword_list_keywords",
        "brand_radar_reports", "create_brand_radar_report", "brand_radar_prompts",
        "create_brand_radar_prompts", "web_analytics", "gsc_report",
        "gsc_anonymous_queries", "social_channels", "social_channel_metrics",
        "social_authors", "social_activity_history", "social_posts",
        "social_post_metrics", "create_social_post", "crawler_ips",
        "crawler_ip_ranges", "domain_rating_free", "domain_rating_top_domains",
    )
    for meth in directly_called:
        assert callable(getattr(AhrefsClient, meth, None)), f"AhrefsClient.{meth} manquant"


def test_no_tool_reaches_a_destructive_management_write():
    """Guide Silae : le client porte delete/patch en entier, aucun tool ne les
    atteint — supprimer/republier est un acte délibéré, jamais un effet de bord."""
    import ast
    import inspect

    src = inspect.getsource(ahrefs)
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    forbidden = {"delete_projects", "update_project", "delete_project_keywords",
                 "untag_project_keywords", "delete_project_competitors",
                 "delete_keyword_list_keywords", "delete_brand_radar_prompts",
                 "update_brand_radar_report", "delete_social_post", "update_social_post"}
    leaked = forbidden & called
    assert not leaked, f"tool(s) reach destructive client method(s): {leaked}"


# --- dispatch report/op (comportement) ---------------------------------------------

def test_site_explorer_dispatches_and_validates_required():
    m, cls, patcher = _fn_with_mock_client()
    try:
        inst = cls.return_value
        inst.domain_rating.return_value = {"domain_rating": 42}
        fn = asyncio.run(m.get_tool("ahrefs_site_explorer")).fn

        result = fn(report="domain-rating", target="example.com", date="2026-01-01")
        assert result == {"domain_rating": 42}
        inst.domain_rating.assert_called_once_with(target="example.com", date="2026-01-01")

        with pytest.raises(McpError, match="requires date"):
            fn(report="domain-rating", target="example.com")

        with pytest.raises(McpError, match="unknown"):
            fn(report="not-a-real-report", target="example.com")
    finally:
        patcher.stop()


def test_site_explorer_applies_default_select_and_extra_overrides():
    m, cls, patcher = _fn_with_mock_client()
    try:
        inst = cls.return_value
        inst.organic_keywords.return_value = {}
        fn = asyncio.run(m.get_tool("ahrefs_site_explorer")).fn

        fn(report="organic-keywords", target="example.com", date="2026-01-01")
        kwargs = inst.organic_keywords.call_args.kwargs
        assert kwargs["select"] == ahrefs._DEFAULT_SELECT[("site_explorer", "organic-keywords")]

        fn(report="organic-keywords", target="example.com", date="2026-01-01",
           select="keyword", extra={"limit": 5})
        kwargs = inst.organic_keywords.call_args.kwargs
        assert kwargs["select"] == "keyword"
        assert kwargs["limit"] == 5
    finally:
        patcher.stop()


def test_project_create_validates_required_and_list_takes_filters():
    m, cls, patcher = _fn_with_mock_client()
    try:
        inst = cls.return_value
        inst.list_projects.return_value = {"projects": []}
        fn = asyncio.run(m.get_tool("ahrefs_project")).fn

        fn(op="list", project_id=7)
        inst.list_projects.assert_called_once_with(project_id=7)

        with pytest.raises(McpError, match="requires project_name"):
            fn(op="create")

        inst.create_project.return_value = {"project_id": 1}
        fn(op="create", project_name="Site", url="https://example.com", mode="domain",
           protocol="both")
        inst.create_project.assert_called_once_with(
            "Site", "https://example.com", "domain", "both")
    finally:
        patcher.stop()


def test_brand_radar_post_report_coerces_csv_to_list():
    m, cls, patcher = _fn_with_mock_client()
    try:
        inst = cls.return_value
        inst.brand_citations_overview.return_value = {}
        fn = asyncio.run(m.get_tool("ahrefs_brand_radar")).fn

        fn(report="citations-overview", data_source="chatgpt,gemini", select="brand,count")
        kwargs = inst.brand_citations_overview.call_args.kwargs
        assert kwargs["data_source"] == ["chatgpt", "gemini"]
        assert kwargs["select"] == ["brand", "count"]
    finally:
        patcher.stop()


def test_gsc_anonymous_queries_has_different_required_shape():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("ahrefs_gsc")).fn

        with pytest.raises(McpError, match="requires `select` and `country`"):
            fn(report="anonymous-queries", date_from="2026-01-01", project_id=1)

        with pytest.raises(McpError, match="requires `project_id`"):
            fn(report="keywords", date_from="2026-01-01")
    finally:
        patcher.stop()


def test_web_analytics_schema_enum_matches_client_report_set(all_tools):
    """`report` est un Literal — FastMCP le publie en enum au schéma, donc un
    appel réel (passé par le protocole, contrairement à `.fn()` direct dans les
    autres tests) est rejeté AVANT le tool. Le client revalide aussi (cf.
    test_ahrefs_client.py::test_web_analytics_rejects_unknown_report) car `.fn()`
    appelé directement — comme un dispatch interne — contourne le schéma."""
    from oto.tools.ahrefs.client import AhrefsClient

    schema = all_tools["ahrefs_web_analytics"].parameters
    enum = set(schema["properties"]["report"]["enum"])
    assert enum == AhrefsClient.WEB_ANALYTICS_REPORTS


def test_site_explorer_rejects_country_on_reports_that_dont_take_it():
    """`domain-rating` n'a pas de `country` dans sa doc Ahrefs — le laisser
    passer serait un silence qui rend un résultat plausible mais faux (agent
    croit avoir scopé un marché, Ahrefs ignore le param ou 400 de façon
    confuse). Vérifié AVANT que `_call_report` n'atteigne le client mocké."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("ahrefs_site_explorer")).fn
        with pytest.raises(McpError, match="has no `country` parameter"):
            fn(report="domain-rating", target="example.com", date="2026-01-01", country="fr")
        cls.return_value.domain_rating.assert_not_called()

        # organic-keywords, lui, ACCEPTE country — ne doit pas être refusé.
        cls.return_value.organic_keywords.return_value = {}
        fn(report="organic-keywords", target="example.com", date="2026-01-01", country="fr")
        assert cls.return_value.organic_keywords.call_args.kwargs["country"] == "fr"
    finally:
        patcher.stop()


# --- corrections issues d'une revue contre le spec OpenAPI réel (docs.ahrefs.com/
# openapi.json, 2026-08-20) — trois formes de corps vérifiées fausses en doc-résumé,
# corrigées ici, verrouillées par ces tests.

def test_brand_radar_post_report_refuses_shape_incompatible_params():
    """`brand`/`competitors`/`where`/`date` n'ont pas la même forme (ou n'existent
    pas) côté POST citations-overview/citations-history — les laisser passer
    silencieusement enverrait un champ ignoré ou mal typé, pas une erreur claire."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("ahrefs_brand_radar")).fn
        for kwargs in (
            dict(report="citations-overview", data_source="chatgpt", select="brand", brand="Nike"),
            dict(report="citations-overview", data_source="chatgpt", select="brand", competitors="Adidas"),
            dict(report="citations-overview", data_source="chatgpt", select="brand", where="foo>1"),
            dict(report="citations-history", data_source="chatgpt", date_from="2026-01-01", date="2026-01-01"),
        ):
            with pytest.raises(McpError, match="POST.*does not accept"):
                fn(**kwargs)
        cls.return_value.brand_citations_overview.assert_not_called()
        cls.return_value.brand_citations_history.assert_not_called()

        # country, lui, se convertit proprement CSV -> liste (même shape sémantique).
        cls.return_value.brand_citations_overview.return_value = {}
        fn(report="citations-overview", data_source="chatgpt,gemini", select="brand,count", country="fr,de")
        kwargs = cls.return_value.brand_citations_overview.call_args.kwargs
        assert kwargs["country"] == ["fr", "de"]
    finally:
        patcher.stop()


def test_project_keywords_add_requires_locations_now():
    """Ahrefs attend deux tableaux PARALLÈLES (`keywords`+`locations`), pas un
    seul tableau enrichi — corrigé après lecture du spec OpenAPI réel."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("ahrefs_project_keywords")).fn
        with pytest.raises(McpError, match="requires .keywords. and .locations."):
            fn(project_id=1, op="add", keywords=[{"keyword": "seo tools"}])

        cls.return_value.add_project_keywords.return_value = {}
        fn(project_id=1, op="add", keywords=[{"keyword": "seo tools"}],
           locations=[{"country": "fr"}])
        cls.return_value.add_project_keywords.assert_called_once_with(
            1, [{"keyword": "seo tools"}], [{"country": "fr"}])
    finally:
        patcher.stop()


def test_brand_radar_report_create_has_no_top_level_data_source():
    """Le corps POST /management/brand-radar-reports n'a PAS de champ `data_source`/
    `frequency` séparé — seul `prompts_frequency` en porte (corrigé après lecture
    du spec OpenAPI réel)."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = asyncio.run(m.get_tool("ahrefs_brand_radar_report")).fn
        with pytest.raises(McpError, match="requires .prompts_frequency."):
            fn(op="create")

        cls.return_value.create_brand_radar_report.return_value = {}
        pf = [{"data_source": "chatgpt", "frequency": "daily"}]
        fn(op="create", prompts_frequency=pf, name="My report")
        cls.return_value.create_brand_radar_report.assert_called_once_with(pf, name="My report")
    finally:
        patcher.stop()
