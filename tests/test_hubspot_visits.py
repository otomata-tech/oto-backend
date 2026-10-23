"""HubSpot website visits — `hubspot_visits`, and what the rest of the connector
gained to serve it (`sorts` on a search, pages of a list search, the portal the
connection test names).

Why this exists: asked "which companies visited the site", an agent had to know the
name of a property no label hints at, could not order a search by date, and read an
EMPTY answer as "nobody came" when it meant "this portal tracks no company". The
tool names the three sources, orders them latest first, and explains its own
emptiness.

Everything is simulated here: no call leaves for a portal. The shapes come from
HubSpot's schema and reference — no portal with tracking on has been read yet, and
the module says so.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import hubspot_scopes as S
from oto_mcp.tools import hubspot_visits as V


# --- simulated HubSpot answers -----------------------------------------------------

#: A search record as HubSpot serves it: nested, with its own three properties and
#: the empty ones as null.
COMPANY = {"id": "501", "createdAt": "2025-01-01T00:00:00Z", "archived": False,
           "url": "https://app.hubspot.example/record/0-2/501", "properties": {
    "name": "Acme", "domain": "acme.example",
    "hs_most_recent_de_anonymized_visit": "2026-10-12T09:30:00Z",
    "hs_intent_visitors_last_30_days": "3", "hs_intent_page_views_last_30_days": "11",
    "hs_recent_intent_signals": None, "hs_intent_signals_enabled": "true",
    "createdate": "2025-01-01T00:00:00Z", "hs_lastmodifieddate": "2026-10-12T09:31:00Z",
    "hs_object_id": "501"}}

PAGE_VIEW = {
    "objectType": "CONTACT", "objectId": "42", "eventType": "e_visited_page",
    "occurredAt": "2026-10-12T09:31:07.123Z", "id": "evt-1",
    "properties": {"hs_url": "https://site.example/pricing", "hs_url_path": "/pricing",
                   "hs_page_title": "Pricing", "hs_browser": "Firefox"}}

MISSING_BI = {"status": "error", "category": "MISSING_SCOPES",
              "message": "The scope needed for this API call isn't available for public use."}


def _upstream(status, body):
    from oto.tools.common.errors import UpstreamHTTPError
    e = UpstreamHTTPError(status, str(body), service="hubspot")
    e.body = body
    return e


@pytest.fixture
def client(monkeypatch):
    import oto.tools.hubspot.client as hs

    inst = MagicMock()
    inst.search_objects.return_value = {"total": 0, "results": []}
    monkeypatch.setattr(hs, "HubSpotClient", lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.auth.hubspot.bearer", lambda *a, **k: "k")
    return inst


def _tool(name: str = "hubspot_visits", module=V):
    from fastmcp import FastMCP

    m = FastMCP("t")
    module.register(m)
    return asyncio.run(m.get_tool(name)).fn


# --- the dates a caller holds --------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("2026-10-06", datetime(2026, 10, 6, tzinfo=timezone.utc)),
    ("2026-10-06T08:00:00Z", datetime(2026, 10, 6, 8, tzinfo=timezone.utc)),
    ("2026-10-06T10:00:00+02:00", datetime(2026, 10, 6, 8, tzinfo=timezone.utc)),
    ("1791273600000", datetime(2026, 10, 6, 8, tzinfo=timezone.utc)),   # milliseconds
    (1791273600, datetime(2026, 10, 6, 8, tzinfo=timezone.utc)),        # seconds
])
def test_since_reads_iso_and_epochs_as_the_same_instant(value, expected):
    assert V._instant(value, "since") == expected


@pytest.mark.parametrize("value", ["last week", "20261006", "2026", "06/10/2026"])
def test_a_value_that_is_not_a_date_is_refused_not_read_as_1970(client, value):
    """`20261006` is a float: read as an epoch it opens the window in 1970 and
    returns the whole portal as "new since"."""
    with pytest.raises(McpError, match="not a date"):
        _tool()(op="companies", since=value)
    client.search_objects.assert_not_called()


# --- companies: Buyer Intent ---------------------------------------------------------

def test_companies_are_searched_on_the_visit_property_latest_first(client):
    client.search_objects.return_value = {"total": 1, "results": [COMPANY]}
    out = _tool()(op="companies", since="2026-10-06T08:00:00Z", limit=25)
    args, kw = client.search_objects.call_args
    assert args == ("companies",)
    assert kw["filters"] == [{"propertyName": V.COMPANY_VISIT, "operator": "GT",
                              "value": "1791273600000"}]
    assert kw["sorts"] == [{"propertyName": V.COMPANY_VISIT,
                            "direction": "DESCENDING"}]
    assert kw["properties"] == list(V.COMPANY_PROPS) and kw["limit"] == 25
    assert out["source"] == "buyer_intent" and out["total"] == 1
    assert out["results"] == [{
        "id": "501", "name": "Acme", "domain": "acme.example",
        "hs_most_recent_de_anonymized_visit": "2026-10-12T09:30:00Z",
        "hs_intent_visitors_last_30_days": "3",
        "hs_intent_page_views_last_30_days": "11",
        "hs_intent_signals_enabled": "true",
        "url": "https://app.hubspot.example/record/0-2/501"}], \
        "a flat row: no null, none of HubSpot's own three properties"
    assert "diagnosis" not in out, "a filled page needs no explanation"
    assert client.search_objects.call_count == 1


def test_full_serves_hubspot_s_records_untouched(client):
    client.search_objects.return_value = {"total": 1, "results": [COMPANY]}
    assert _tool()(op="companies", full=True)["results"] == [COMPANY]
    assert _tool()(op="contacts", full=True)["results"] == [COMPANY]


def test_an_extra_column_asked_for_is_served_in_the_row(client):
    record = {"id": "1", "properties": {"name": "Acme", "industry": "SOFTWARE"}}
    client.search_objects.return_value = {"total": 1, "results": [record]}
    assert _tool()(op="companies", properties=["industry"])["results"] == [
        {"id": "1", "name": "Acme", "industry": "SOFTWARE"}]


def test_without_a_window_the_search_still_filters_on_having_a_visit(client):
    """No filter at all would return every company of the portal, visited or not."""
    _tool()(op="companies")
    assert client.search_objects.call_args_list[0].kwargs["filters"] == [
        {"propertyName": V.COMPANY_VISIT, "operator": "HAS_PROPERTY"}]


def test_a_window_can_be_closed_and_extra_columns_are_added_once(client):
    _tool()(op="companies", since="2026-10-01", until="2026-10-06",
            properties=["industry", "domain"])
    kw = client.search_objects.call_args_list[0].kwargs
    assert [f["operator"] for f in kw["filters"]] == ["GT", "LTE"]
    assert kw["properties"] == list(V.COMPANY_PROPS) + ["industry"]


def test_an_empty_portal_says_that_nothing_is_tracked(client):
    """The case observed: the Buyer Intent schema present, not one value. The empty
    answer names the cause and what turns it on, in one more search."""
    out = _tool()(op="companies")
    assert out["results"] == []
    assert out["diagnosis"]["tracked_companies"] == 0
    assert "HubSpot Credits" in out["diagnosis"]["note"]
    second = client.search_objects.call_args_list[1]
    assert second.kwargs["filters"] == [
        {"propertyName": V.COMPANY_TRACKED, "operator": "EQ", "value": "true"}]


def test_tracked_companies_without_a_visit_are_not_called_untracked(client):
    client.search_objects.side_effect = [{"total": 0, "results": []},
                                         {"total": 37, "results": [{"id": "1"}]}]
    out = _tool()(op="companies", since="2026-10-06")
    assert out["diagnosis"]["tracked_companies"] == 37
    assert "in this window" in out["diagnosis"]["note"]
    assert "Credits" not in out["diagnosis"]["note"]


def test_an_empty_page_behind_a_cursor_is_not_diagnosed(client):
    out = _tool()(op="companies", after="100")
    assert "diagnosis" not in out and client.search_objects.call_count == 1


def test_a_diagnosis_that_fails_does_not_break_the_empty_answer(client, caplog):
    client.search_objects.side_effect = [{"total": 0, "results": []},
                                         _upstream(400, {"message": "no such property"})]
    with caplog.at_level("WARNING", logger=V.__name__):
        out = _tool()(op="companies")
    assert out["results"] == [] and out["diagnosis"]["tracked_companies"] is None
    assert "not explained" in out["diagnosis"]["note"]
    assert "tracking diagnosis failed" in caplog.text, "a failed diagnosis is logged"


# --- contacts: the people HubSpot already knows ----------------------------------------

def test_contacts_are_searched_on_their_last_session(client):
    client.search_objects.return_value = {"total": 1, "results": [
        {"id": "42", "properties": {"email": "a@b.example", "lastname": None,
                                    "hs_analytics_last_url": "https://site.example/"}}]}
    out = _tool()(op="contacts", since="2026-10-06")
    args, kw = client.search_objects.call_args
    assert args == ("contacts",)
    assert kw["filters"][0]["propertyName"] == V.CONTACT_VISIT
    assert kw["sorts"] == [{"propertyName": V.CONTACT_VISIT,
                            "direction": "DESCENDING"}]
    assert "hs_analytics_last_url" in kw["properties"]
    assert out["source"] == "known_contacts"
    assert out["results"] == [{"id": "42", "email": "a@b.example",
                               "hs_analytics_last_url": "https://site.example/"}]


def test_an_empty_contact_answer_carries_no_tracking_diagnosis(client):
    """Buyer Intent tracking has nothing to do with known contacts."""
    out = _tool()(op="contacts")
    assert "diagnosis" not in out and client.search_objects.call_count == 1


# --- page views: the events API --------------------------------------------------------

def test_page_views_read_one_contact_s_events_in_a_window(client):
    client._request.return_value = {"results": [PAGE_VIEW],
                                    "paging": {"next": {"after": "c2"}}}
    out = _tool()(op="page_views", object_id="42", since="2026-10-06T08:00:00Z",
                  limit=500, after="c1")
    (method, path), kw = client._request.call_args
    assert (method, path) == ("GET", "/events/v3/events")
    assert kw["params"] == {
        "objectType": "contact", "objectId": "42", "eventType": "e_visited_page",
        "limit": 100, "occurredAfter": "2026-10-06T08:00:00.000Z", "after": "c1"}
    assert out["results"] == [{
        "occurredAt": "2026-10-12T09:31:07.123Z", "url": "https://site.example/pricing",
        "path": "/pricing", "title": "Pricing", "id": "evt-1"}]
    assert out["paging"] == {"next": {"after": "c2"}}


def test_full_serves_hubspot_s_events_untouched(client):
    client._request.return_value = {"results": [PAGE_VIEW]}
    assert _tool()(op="page_views", object_id="42", full=True) == {
        "results": [PAGE_VIEW]}


def test_an_event_without_a_url_is_served_without_inventing_one():
    assert V.compact_event({"occurredAt": "t", "id": "e", "properties": {}}) == {
        "occurredAt": "t", "id": "e"}
    assert V.compact_event({"properties": {"hs_base_url": "https://b.example"}}) == {
        "url": "https://b.example"}


def test_another_object_type_is_passed_as_given(client):
    """Only contacts are documented; a raw objectTypeId is HubSpot's to judge."""
    client._request.return_value = {"results": []}
    _tool()(op="page_views", object_id="7", object_type="0-2")
    assert client._request.call_args.kwargs["params"]["objectType"] == "0-2"
    _tool()(op="page_views", object_id="7", object_type="companies")
    assert client._request.call_args.kwargs["params"]["objectType"] == "company"


def test_page_views_need_a_record(client):
    with pytest.raises(McpError, match="requires object_id"):
        _tool()(op="page_views")
    client._request.assert_not_called()


def test_event_types_lists_what_the_portal_defines(client):
    client._request.return_value = {"eventTypes": ["e_visited_page"]}
    assert _tool()(op="event_types") == {"eventTypes": ["e_visited_page"]}
    assert client._request.call_args.args == ("GET", "/events/v3/events/event-types")


# --- refusals ----------------------------------------------------------------------

def test_a_missing_events_scope_names_business_intelligence(client):
    client._request.side_effect = _upstream(403, MISSING_BI)
    with pytest.raises(McpError) as e:
        _tool()(op="page_views", object_id="42")
    assert "`business-intelligence`" in str(e.value)
    assert e.value.oto_detail["family"] == "events"
    assert e.value.oto_detail["scope_to_add"] == "business-intelligence"


def test_another_403_on_events_points_at_the_plan_and_quotes_hubspot(client):
    """Not a checkbox: HubSpot's reference lists the events API for Professional
    and Enterprise. The status stays 403 — the envelope classifies on it."""
    from oto.tools.common.errors import UpstreamHTTPError

    client._request.side_effect = _upstream(
        403, {"status": "error", "message": "This account is not allowed here."})
    with pytest.raises(UpstreamHTTPError) as e:
        _tool()(op="page_views", object_id="42")
    assert e.value.status_code == 403
    assert "Professional" in str(e.value) and "not allowed here" in str(e.value)
    assert "op='contacts'" in str(e.value)


def test_a_missing_companies_scope_still_names_the_companies_scope(client):
    client.search_objects.side_effect = _upstream(403, MISSING_BI)
    with pytest.raises(McpError) as e:
        _tool()(op="companies")
    assert e.value.oto_detail["scope_to_add"] == "crm.objects.companies.read"


def test_a_scope_refusal_outside_missing_scopes_names_what_hubspot_asks(client):
    """Served on a real portal, 2026-10-14, to a private app token that HOLDS
    `business-intelligence`: the message verbatim (the rest of the body was not
    kept). The first version read it as "not a scope, so the plan" — wrong on both
    counts, and it sent the reader to check a plan."""
    from oto.tools.common.errors import UpstreamHTTPError

    client._request.side_effect = _upstream(403, {
        "status": "error", "category": "FORBIDDEN",
        "message": "Insufficient scopes, requires one of: [event-detail-read,[id]]"})
    with pytest.raises(UpstreamHTTPError) as e:
        _tool()(op="page_views", object_id="42")
    msg = str(e.value)
    assert e.value.status_code == 403
    assert "`event-detail-read`" in msg and "published scope list" in msg
    assert "Professional" not in msg, "a scope refusal is not a plan refusal"
    assert "op='contacts'" in msg
    assert e.value.oto_detail["reason"] == "permission_not_grantable"
    assert e.value.oto_detail["hubspot_asks"] == ["event-detail-read"]


def test_a_scope_refusal_naming_a_known_scope_says_which_one_to_tick(client):
    """HubSpot names a scope a private app CAN tick: a checkbox, not "nothing to
    tick" — whatever category it filed the 403 under."""
    client._request.side_effect = _upstream(403, {
        "status": "error", "category": "FORBIDDEN",
        "message": "Insufficient scopes, requires one of: [business-intelligence]"})
    with pytest.raises(McpError) as e:
        _tool()(op="page_views", object_id="42")
    msg = str(e.value)
    assert "Scope to add: `business-intelligence`" in msg
    assert "published scope list" not in msg and "not enough" not in msg
    assert e.value.oto_detail["reason"] == "missing_scopes"
    assert e.value.oto_detail["scope_to_add"] == "business-intelligence"
    assert e.value.oto_detail["scopes_source"] == "hubspot"


def test_a_scope_refusal_naming_nothing_makes_no_claim_about_the_list(client):
    """"Scope" in HubSpot's words, no name: nothing to point at, and nothing allows
    saying the permission is outside the published list."""
    from oto.tools.common.errors import UpstreamHTTPError

    client._request.side_effect = _upstream(403, {
        "status": "error", "category": "FORBIDDEN",
        "message": "This token does not have the required scopes."})
    with pytest.raises(UpstreamHTTPError) as e:
        _tool()(op="page_views", object_id="42")
    msg = str(e.value)
    assert e.value.status_code == 403
    assert "scope reason it does not name" in msg
    assert "published scope list" not in msg and "Professional" not in msg
    assert "required scopes" in msg, "HubSpot's own words are quoted"
    assert e.value.oto_detail["reason"] == "scope_refused"


@pytest.mark.parametrize("message,expected", [
    ("Insufficient scopes, requires one of: [event-detail-read,[id]]",
     ["event-detail-read"]),
    ("Insufficient scopes, requires one of: [a.read, b-read]", ["a.read", "b-read"]),
    ("This account is not allowed here.", []),
    (None, []),
])
def test_permissions_are_read_from_the_message_placeholders_left_out(message, expected):
    assert S.permissions_in_message(message) == expected


def test_the_events_scope_is_named_but_never_measured():
    """Measured, `business-intelligence` would read "events scope missing" on the
    connection test of every portal that never asked for page views."""
    assert "events" in S.OPTIONAL_SCOPES
    assert "events" not in S.SCOPES and "events" not in S._PROBES
    assert "events" not in S._granted_from_list({"crm.objects.contacts.read"})


def test_an_unknown_op_and_a_bad_properties_shape_are_refused(client):
    with pytest.raises(McpError, match="op must be"):
        _tool()(op="visitors")
    with pytest.raises(McpError, match="list of INTERNAL property names"):
        _tool()(op="companies", properties={"name": "x"})


# --- the tool declares what it is --------------------------------------------------

def test_the_tool_is_declared_read_only_and_mounted_with_the_connector():
    from fastmcp import FastMCP
    from oto_mcp.providers.hubspot import CONNECTOR
    from oto_mcp.tools.lecture import en_lecture

    m = FastMCP("t")
    V.register(m)
    assert en_lecture(asyncio.run(m.get_tool("hubspot_visits")))
    assert "hubspot_visits" in CONNECTOR.modules


# --- what the rest of the connector gained -------------------------------------------

def test_a_search_can_be_ordered(client):
    from oto_mcp.tools import hubspot as H

    sorts = [{"propertyName": "createdate", "direction": "DESCENDING"}]
    _tool("hubspot_object", H)(op="search", object_type="companies", sorts=sorts)
    assert client.search_objects.call_args.kwargs["sorts"] == sorts


def test_a_second_sorting_rule_is_refused_not_sent(client):
    """HubSpot applies one rule: whichever it kept, the order would not be the
    one asked for."""
    from oto_mcp.tools import hubspot as H

    two = [{"propertyName": "a", "direction": "ASCENDING"},
           {"propertyName": "b", "direction": "DESCENDING"}]
    with pytest.raises(McpError, match="ONE rule"):
        _tool("hubspot_object", H)(op="search", object_type="companies", sorts=two)
    with pytest.raises(McpError, match="propertyName"):
        _tool("hubspot_object", H)(op="search", object_type="companies",
                                   sorts=["createdate"])
    with pytest.raises(McpError, match="does not accept sorts"):
        _tool("hubspot_object", H)(op="list", object_type="companies",
                                   sorts=two[:1])
    client.search_objects.assert_not_called()
    client.list_objects.assert_not_called()


def test_a_list_search_can_be_paged_and_narrowed_by_kind(client):
    from oto_mcp.tools import hubspot as H

    _tool("hubspot_list", H)(op="search", object_type="companies", offset=20,
                             count=50, processing_types=["dynamic"])
    assert client.search_lists.call_args.kwargs == {
        "query": None, "object_type_id": "0-2", "processing_types": ["DYNAMIC"],
        "count": 50, "offset": 20}


def test_search_only_arguments_are_refused_on_the_other_list_ops(client):
    from oto_mcp.tools import hubspot as H

    with pytest.raises(McpError, match="does not accept offset"):
        _tool("hubspot_list", H)(op="members", list_id="9", offset=20)
    with pytest.raises(McpError, match="MANUAL, DYNAMIC"):
        _tool("hubspot_list", H)(op="search", processing_types=["static"])
    client.get_list_memberships.assert_not_called()
    client.search_lists.assert_not_called()
