"""Connecteur PostHog — analytics produit (API privée REST + /query/).

Verrouille : l'entrée de registre (credential à 3 champs dont la région, byo-only,
catégorie Dev, éditeur PostHog), la doc how-to, la surface MCP (8 tools, chacun
avec une description), la sonde « tester la connexion », la jointure tool↔client
oto-core, le dispatch `op=`, et les trois propriétés qui font la valeur de ce
connecteur : **la réponse de /query/ est projetée** (93 % de bruit sinon), **les
entonnoirs ne se réécrivent pas en SQL**, et **rien ne change le produit**.
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import posthog

EXPECTED_TOOLS = {
    "posthog_query", "posthog_schema", "posthog_person", "posthog_group",
    "posthog_insight", "posthog_flag", "posthog_recording", "posthog_project",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_credential(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: {"api_key": "phx_x",
                                        "host": "https://eu.posthog.com",
                                        "project_id": "42"})


def _fn_with_mock_client():
    from fastmcp import FastMCP

    patcher = patch("oto.tools.posthog.client.PostHogClient")
    cls = patcher.start()
    m = FastMCP("t")
    posthog.register(m)
    return m, cls, patcher


def _tool(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --- registre -----------------------------------------------------------------

def test_posthog_is_a_byo_only_multi_field_connector():
    c = providers.REGISTRY["posthog"]
    assert c.kind == "tools"
    assert c.secret_kind == "fields"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "platform" not in c.auth_modes, "les données produit du client, jamais mutualisées"
    assert c.default_active is False
    assert "posthog" in providers.CREDENTIAL_PROVIDERS
    assert "posthog" not in providers.KEY_PROVIDERS


def test_region_is_a_config_field_because_us_and_eu_are_separate_deployments():
    """Une clé US est inconnue côté EU, et le symptôme est un 401 — pas un
    message de région. Le host doit donc être posé, jamais deviné."""
    c = providers.REGISTRY["posthog"]
    assert [f.name for f in c.secret_fields] == ["api_key", "host", "project_id"]
    assert [f.name for f in c.config_fields] == ["host", "project_id"]
    assert c.secret_fields[0].secret is True
    assert all(not f.required for f in c.config_fields)


def test_posthog_catalogue_overlays_are_curated():
    c = providers.REGISTRY["posthog"]
    assert c.category == "Product & data"
    assert c.publisher_name == "PostHog"
    assert c.label == "PostHog"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["posthog"] == "posthog.com"
    assert "posthog" not in providers._SANS_LOGO_DE_MARQUE


def test_posthog_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["posthog"].doc_sections}
    assert {"prerequisite", "usage", "note"} <= kinds


def test_the_doc_warns_about_the_project_key_trap():
    """`phc_` est la clé que PostHog met le plus en avant et l'API la refuse —
    si la doc ne le dit pas, tout le monde s'y cogne."""
    body = "\n".join(s.body_md for s in providers.REGISTRY["posthog"].doc_sections)
    assert "phc_" in body and "phx_" in body


# --- surface MCP --------------------------------------------------------------

def test_posthog_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "posthog" for t in all_tools if t.startswith("posthog_"))


def test_posthog_exposes_exactly_the_expected_tools(all_tools):
    assert {t for t in all_tools if t.startswith("posthog_")} == EXPECTED_TOOLS


def test_posthog_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    # ⚠️ le patch DOIT être arrêté : sans ça la classe cliente reste un
    # MagicMock pour les tests suivants, et `hasattr(mock, "…")` étant
    # toujours vrai, les tests d'absence ne prouveraient plus rien.
    m, cls, patcher = _fn_with_mock_client()
    try:
        assert connector_verify.supports("posthog")
    finally:
        patcher.stop()


def test_verify_exercises_a_real_query_not_just_identity():
    """Une clé sans `query:read` s'authentifie parfaitement puis échoue au
    premier appel réel : une sonde qui s'arrête à l'identité dirait « OK » là où
    l'outil phare est inutilisable."""
    with patch("oto.tools.posthog.client.PostHogClient") as cls:
        posthog._verify({"api_key": "phx_x"}, {"host": None, "project_id": None})
        cls.return_value.current_user.assert_called_once()
        cls.return_value.query.assert_called_once()

    with patch("oto.tools.posthog.client.PostHogClient") as cls:
        cls.return_value.query.side_effect = RuntimeError("403 Forbidden")
        cls.return_value.resolve_project_id.return_value = "42"
        with pytest.raises(RuntimeError, match="query:read"):
            posthog._verify({"api_key": "phx_x"}, None)


# --- jointure tool ↔ client oto-core -------------------------------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.posthog.client import PostHogClient
    for meth in ("query", "run_query", "run_insight", "database_schema",
                 "current_user", "list_projects", "resolve_project_id",
                 "list_event_definitions", "list_property_definitions",
                 "list_property_values", "list_persons", "get_person",
                 "list_person_activity", "list_cohorts", "list_cohort_persons",
                 "list_group_types", "list_groups", "find_group",
                 "list_insights", "get_insight", "list_dashboards", "get_dashboard",
                 "list_feature_flags", "get_feature_flag", "list_experiments",
                 "get_experiment", "list_session_recordings", "get_session_recording",
                 "list_annotations", "create_annotation"):
        assert callable(getattr(PostHogClient, meth, None)), f"PostHogClient.{meth} manquant"


def test_no_product_changing_write_is_reachable():
    from oto.tools.posthog.client import PostHogClient
    for absent in ("create_feature_flag", "update_feature_flag", "toggle_feature_flag",
                   "delete_person", "create_insight", "update_insight",
                   "create_cohort", "delete_session_recording", "capture"):
        assert not hasattr(PostHogClient, absent), f"PostHogClient.{absent} est réapparu"


# --- la projection de /query/ : le budget de réponse ---------------------------

def _raw_query_response():
    """La forme RÉELLE mesurée le 2026-08-22 — 2 525 caractères pour deux cellules."""
    return {
        "columns": ["a", "b"], "types": [["a", "UInt8"], ["b", "String"]],
        "results": [[1, "x"]], "hasMore": False,
        "hogql": "SELECT 1 AS a, 'x' AS b LIMIT 101 OFFSET 0",
        "error": None, "explain": None,
        "clickhouse": "SELECT 1 AS a, %(hogql_val_0)s AS b LIMIT 101 " + "x" * 450,
        "modifiers": {"bounceRateDurationSeconds": None, "junk": "y" * 1100},
        "cache_key": "cache_424242_" + "z" * 60, "is_cached": False,
        "timezone": "UTC", "limit": 100, "offset": 0, "query_status": None,
    }


def test_query_response_is_projected_down_to_what_matters():
    kept = posthog._projeter(_raw_query_response())
    assert set(kept) == {"columns", "types", "results", "hasMore", "hogql"}
    for noise in ("clickhouse", "modifiers", "cache_key", "timezone", "is_cached"):
        assert noise not in kept, f"{noise} gonfle la réponse sans rien apprendre"


def test_projection_is_a_real_budget_saving():
    import json
    raw = _raw_query_response()
    before = len(json.dumps(raw))
    after = len(json.dumps(posthog._projeter(raw)))
    assert after < before / 5, f"projection insuffisante : {before} → {after}"


def test_truncation_is_announced_not_hidden():
    raw = _raw_query_response()
    raw["hasMore"] = True
    kept = posthog._projeter(raw)
    assert "TRUNCATED" in kept["note"].upper()


# --- posthog_query : les deux voies, exclusives --------------------------------

def test_query_requires_exactly_one_of_hogql_or_typed_query():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "posthog_query")
        with pytest.raises(McpError, match="EXACTLY one"):
            fn()
        with pytest.raises(McpError, match="EXACTLY one"):
            fn(hogql="SELECT 1", query={"kind": "TrendsQuery"})
    finally:
        patcher.stop()


def test_hogql_goes_through_query_and_typed_goes_through_run_query():
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.query.return_value = _raw_query_response()
        cls.return_value.run_query.return_value = _raw_query_response()
        fn = _tool(m, "posthog_query")

        out = fn(hogql="SELECT count() FROM events")
        cls.return_value.query.assert_called_once()
        assert "clickhouse" not in out, "la réponse doit être projetée"

        fn(query={"kind": "FunnelsQuery", "series": []})
        cls.return_value.run_query.assert_called_once()
    finally:
        patcher.stop()


def test_query_docstring_teaches_the_dialect(all_tools):
    """Sans ça un LLM écrit du JSONExtract, compare des chaînes comme des nombres
    et compte des appareils au lieu d'utilisateurs — trois erreurs qui rendent un
    nombre plausible et faux."""
    doc = all_tools["posthog_query"].description
    for must in ("properties.", "toFloat", "uniq(person_id)", "INTERVAL", "FunnelsQuery"):
        assert must in doc, f"le docstring n'enseigne pas {must!r}"


# --- posthog_insight : la voie de confiance ------------------------------------

def test_run_replays_the_saved_insight_with_an_overridden_window():
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.run_insight.return_value = _raw_query_response()
        out = _tool(m, "posthog_insight")(op="run", insight_id="7", date_from="-7d")
        cls.return_value.run_insight.assert_called_once_with(
            "7", date_from="-7d", date_to=None, project_id=None)
        assert "clickhouse" not in out
    finally:
        patcher.stop()


def test_get_refuses_a_date_override_because_it_does_not_execute():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="op='get' does not use"):
            _tool(m, "posthog_insight")(op="get", insight_id="7", date_from="-7d")
    finally:
        patcher.stop()


def test_insight_run_requires_an_id():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="requires `insight_id`"):
            _tool(m, "posthog_insight")(op="run")
    finally:
        patcher.stop()


# --- posthog_schema : bornée par construction ----------------------------------

def test_tables_returns_names_only_not_the_whole_schema():
    """156 tables dont `events` à 52 colonnes : tout rendre d'un coup noierait
    le budget de réponse."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.database_schema.return_value = {
            "tables": {"events": {"fields": {f"c{i}": {"type": "String"}
                                             for i in range(52)}},
                       "persons": {"fields": {}}}}
        out = _tool(m, "posthog_schema")(op="tables")
        assert out["tables"] == ["events", "persons"]
        assert out["count"] == 2
        assert "fields" not in str(out)
    finally:
        patcher.stop()


def test_columns_requires_a_table_and_names_an_unknown_one():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "posthog_schema")
        with pytest.raises(McpError, match="requires `table`"):
            fn(op="columns")
        cls.return_value.database_schema.return_value = {"tables": {"events": {"fields": {}}}}
        with pytest.raises(McpError, match="is unknown to this project"):
            fn(op="columns", table="nope")
    finally:
        patcher.stop()


def test_columns_returns_one_table_with_its_types():
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.database_schema.return_value = {
            "tables": {"events": {"fields": {"event": {"type": "String"},
                                             "timestamp": {"type": "DateTime"}}}}}
        out = _tool(m, "posthog_schema")(op="columns", table="events")
        assert out["columns"] == {"event": "String", "timestamp": "DateTime"}
        assert out["count"] == 2
    finally:
        patcher.stop()


def test_values_requires_a_property_key():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="requires `property_key`"):
            _tool(m, "posthog_schema")(op="values")
    finally:
        patcher.stop()


# --- posthog_group : dire « ce projet n'a pas de comptes » ----------------------

def test_empty_group_types_says_the_project_has_no_account_level():
    """Rendre une liste vide laisserait l'agent répondre par personne à une
    question par compte, sans que rien ne signale la substitution."""
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.list_group_types.return_value = []
        out = _tool(m, "posthog_group")(op="types")
        assert out["group_types"] == []
        assert "does not do group analytics" in out["note"]
    finally:
        patcher.stop()


def test_group_list_requires_the_type_index():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="requires `group_type_index`"):
            _tool(m, "posthog_group")(op="list")
        _tool(m, "posthog_group")(op="list", group_type_index=0, search="acme")
        cls.return_value.list_groups.assert_called_once_with(
            0, project_id=None, search="acme")
    finally:
        patcher.stop()


# --- posthog_person -------------------------------------------------------------

def test_person_get_requires_id_and_refuses_list_filters():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "posthog_person")
        with pytest.raises(McpError, match="requires `person_id`"):
            fn(op="get")
        with pytest.raises(McpError, match="op='get' does not use"):
            fn(op="get", person_id="p1", search="alice")
    finally:
        patcher.stop()


def test_cohort_persons_requires_a_cohort():
    m, cls, patcher = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="requires `cohort_id`"):
            _tool(m, "posthog_person")(op="cohort_persons")
    finally:
        patcher.stop()


# --- posthog_project : la seule écriture ---------------------------------------

def test_annotate_is_the_only_write_and_requires_content():
    m, cls, patcher = _fn_with_mock_client()
    try:
        fn = _tool(m, "posthog_project")
        with pytest.raises(McpError, match="requires `content`"):
            fn(op="annotate")
        fn(op="annotate", content="v2.3 en production", date_marker="2026-08-22T12:00:00Z")
        cls.return_value.create_annotation.assert_called_once_with(
            "v2.3 en production", date_marker="2026-08-22T12:00:00Z", project_id=None)
    finally:
        patcher.stop()


def test_current_reports_which_project_and_account_answered():
    m, cls, patcher = _fn_with_mock_client()
    try:
        cls.return_value.current_user.return_value = {
            "email": "a@acme.test", "organization": {"name": "Acme"}}
        cls.return_value.resolve_project_id.return_value = "424242"
        cls.return_value.host = "https://eu.posthog.com"
        out = _tool(m, "posthog_project")(op="current")
        assert out == {"project_id": "424242", "organization": "Acme",
                       "account": "a@acme.test", "host": "https://eu.posthog.com"}
    finally:
        patcher.stop()


# --- messages d'erreur ----------------------------------------------------------

def test_hogql_error_points_at_the_schema_tool_and_keeps_postgres_detail():
    from oto.tools.common.errors import UpstreamHTTPError
    msg = posthog._upstream_message(UpstreamHTTPError(400, {
        "code": "hogql_query_error",
        "detail": "Unable to resolve field: nope",
        "extra": {"hogql_metadata": {"errors": [{"start": 7, "end": 11}]}}},
        service="posthog"))
    assert "Unable to resolve field: nope" in msg
    assert "posthog_schema" in msg
    assert "position 7-11" in msg


def test_auth_error_names_the_three_real_causes():
    from oto.tools.common.errors import UpstreamHTTPError
    msg = posthog._upstream_message(UpstreamHTTPError(401, {
        "detail": "Personal API key found in request Authorization header is invalid."},
        service="posthog"))
    assert "phx_" in msg and "phc_" in msg
    assert "REGION" in msg
    assert "scopes" in msg
