"""HubSpot rendu conscient de ses scopes, et lisible sur ses pipelines.

Deux portails clients ont fait remonter ce lot :

1. **Les scopes.** `hubspot_object` sur `tickets` rendait `403 MISSING_SCOPES` — la
   doc faisait cocher `crm.objects.tickets`, qui n'est PAS un nom de scope HubSpot.
   La sonde de connexion ne couvrait que `auth` : un portail sans le scope tickets
   s'affichait sain, et le manque n'apparaissait qu'au milieu d'une tâche. Un 404
   revenait en page HTML.
2. **Les pipelines.** `dealstage` sortait avec des `options` VIDES (une étape
   appartient à un pipeline, pas à la propriété), les deals avec des ids d'étape
   bruts, et `hubspot_property op=list` sur deals pesait ~275k caractères.

Tout est simulé ici : aucun appel ne part vers un portail.
"""
from __future__ import annotations

import asyncio
import copy
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import hubspot_scopes as S


# --- réponses HubSpot simulées ---------------------------------------------------

#: Forme documentée (« Using Object APIs ») : la liste veut dire « l'UN de ».
MISSING_TICKETS = {
    "status": "error",
    "message": ("This app hasn't been granted all required scopes to make this call. "
                "Read more about required scopes here: https://developers.hubspot.com/scopes."),
    "errors": [{"message": "One or more of the following scopes are required.",
                "context": {"requiredGranularScopes": [
                    "crm.objects.tickets.sensitive.read.v2",
                    "crm.objects.tickets.read",
                    "crm.objects.tickets.highly_sensitive.read.v2"]}}],
    "category": "MISSING_SCOPES",
}
#: La forme ancienne, sans aucun nom de scope.
MISSING_SANS_NOM = {
    "status": "error",
    "message": "The scope needed for this API call isn't available for public use.",
    "category": "MISSING_SCOPES",
}
HTML_404 = "<html><head><title>404</title></head><body>Not Found</body></html>"

DEAL_PIPELINES = {"results": [
    {"id": "default", "label": "Sales", "displayOrder": 0, "archived": False,
     "createdAt": "x", "stages": [
         {"id": "closedlost", "label": "Closed lost", "displayOrder": 6, "archived": False,
          "metadata": {"probability": "0.0", "isClosed": "true"}, "createdAt": "x"},
         {"id": "appointmentscheduled", "label": "Appointment", "displayOrder": 0,
          "archived": False, "metadata": {"probability": "0.2", "isClosed": "false"}},
         {"id": "old", "label": "Legacy stage", "displayOrder": 3, "archived": True,
          "metadata": {"probability": "0.5"}}]},
    {"id": "77", "label": "Partners", "displayOrder": 1, "archived": False, "stages": [
        {"id": "123456", "label": "Signed", "displayOrder": 0, "archived": False,
         "metadata": {"probability": "1.0", "isClosed": "true"}}]},
]}
TICKET_PIPELINES = {"results": [
    {"id": "0", "label": "Support", "displayOrder": 0, "archived": False, "stages": [
        {"id": "1", "label": "New", "displayOrder": 0, "archived": False,
         "metadata": {"ticketState": "OPEN"}},
        {"id": "4", "label": "Closed", "displayOrder": 3, "archived": False,
         "metadata": {"ticketState": "CLOSED"}}]}]}


def _http(status, body, policy=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body, service="hubspot")


# --- la sonde : auth+scopes --------------------------------------------------------

class _Rep:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content, self.text = b"x", str(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _Session:
    """Répond par chemin ; `token_info` = la réponse de l'introspection (None =
    l'introspection échoue, on retombe sur une lecture par famille)."""

    def __init__(self, token_info=None, refus=()):
        self.token_info, self.refus, self.appels = token_info, set(refus), []

    def request(self, method, url, **kw):
        path = url.replace("https://api.hubapi.com", "")
        self.appels.append((method, path))
        if path.endswith("access-token-info"):
            if self.token_info is None:
                return _Rep(404, HTML_404)
            return _Rep(200, {"scopes": self.token_info})
        fam = next(f for f, (m, p, _) in S._PROBES.items() if p == path)
        if fam in self.refus:
            return _Rep(403, MISSING_SANS_NOM)
        return _Rep(200, {"results": []})


class _Client:
    BASE_URL = "https://api.hubapi.com"

    def __init__(self, session):
        self.session = session


TOUS = ["crm.objects.contacts.read", "crm.objects.companies.read",
        "crm.objects.deals.read", "crm.objects.tickets.read", "tickets",
        "crm.lists.read", "crm.schemas.contacts.read", "crm.objects.owners.read"]


def test_verify_with_every_scope_granted():
    m = S.measure_scopes(_Client(_Session(token_info=TOUS)), "pat-x")
    assert m["method"] == "token_info"
    assert set(m["families"].values()) == {S.GRANTED}
    assert m["missing_scopes"] == {} and m["summary"] == "connected"


def test_verify_with_only_tickets_missing():
    """Le cas du portail client : tout marche sauf tickets — le résumé le dit en
    clair, et nomme le scope GRANULAIRE à cocher, pas `crm.objects.tickets`."""
    sans = [s for s in TOUS if s not in ("crm.objects.tickets.read", "tickets")]
    m = S.measure_scopes(_Client(_Session(token_info=sans)), "pat-x")
    assert m["families"]["tickets"] == S.MISSING
    assert m["families"]["pipelines_tickets"] == S.MISSING
    assert m["families"]["contacts"] == S.GRANTED
    assert m["missing_scopes"]["tickets"][0] == "crm.objects.tickets.read"
    assert "tickets scope" in m["summary"] or "tickets, pipelines_tickets" in m["summary"]
    assert m["summary"].startswith("connected, ")


def test_verify_with_lists_and_schemas_missing_by_per_family_reads():
    """Sans introspection, UNE lecture minimale par famille ; un 403
    MISSING_SCOPES = `missing`, et rien d'autre ne l'est."""
    sess = _Session(token_info=None, refus={"lists", "properties"})
    m = S.measure_scopes(_Client(sess), "pat-x")
    assert m["method"] == "probe"
    assert m["families"]["lists"] == S.MISSING
    assert m["families"]["properties"] == S.MISSING
    assert m["families"]["deals"] == S.GRANTED
    assert "lists, properties scopes missing" in m["summary"]
    # bornée : l'introspection + une lecture par famille, et rien qui écrive
    assert len(sess.appels) == 1 + len(S._PROBES)
    ecritures = [(m_, p) for m_, p in sess.appels
                 if m_ != "GET" and p not in ("/crm/v3/lists/search",
                                              "/oauth/v2/private-apps/get/access-token-info")]
    assert ecritures == []


def test_another_403_is_unknown_not_missing():
    class _S(_Session):
        def request(self, method, url, **kw):
            if "access-token-info" in url:
                return _Rep(404, HTML_404)
            return _Rep(403, {"category": "BANNED"})
    m = S.measure_scopes(_Client(_S()), "pat-x")
    assert set(m["families"].values()) == {S.UNKNOWN}


def test_the_scope_pass_stops_at_its_deadline(monkeypatch):
    monkeypatch.setattr(S, "_TOTAL_S", 0)
    m = S.measure_scopes(_Client(_Session(token_info=None)), "pat-x")
    assert set(m["families"].values()) == {S.UNKNOWN}


# --- les refus ---------------------------------------------------------------------

def test_403_with_scope_names_names_the_plain_one():
    err = S.translate(_http(403, MISSING_TICKETS), object_type="tickets")
    assert isinstance(err, McpError)
    msg = err.error.message
    assert "`crm.objects.tickets.read`" in msg and "sensitive" not in msg
    assert "token does not change" in msg
    assert err.oto_detail["scope_to_add"] == "crm.objects.tickets.read"
    assert err.oto_detail["scopes_source"] == "hubspot"
    assert len(err.oto_detail["accepted_scopes"]) == 3


def test_403_without_scope_names_falls_back_on_the_reference():
    err = S.translate(_http(403, MISSING_SANS_NOM), object_type="tickets")
    assert err.oto_detail["scopes_source"] == "reference"
    assert err.oto_detail["scope_to_add"] == "crm.objects.tickets.read"
    # sur les propriétés de tickets, c'est le scope historique qui est demandé
    err = S.translate(_http(403, MISSING_SANS_NOM), object_type="tickets",
                      family="properties")
    assert err.oto_detail["scope_to_add"] == "tickets"


def test_404_html_becomes_a_clean_not_found_that_keeps_its_status():
    from oto_mcp import error_taxonomy

    err = S.translate(_http(404, HTML_404), object_type="tickets", object_id="42",
                      what="ticket")
    assert "<html" not in str(err) and "'42'" in str(err) and "tickets" in str(err)
    assert err.status_code == 404
    assert error_taxonomy.classify(err).code == "not_found"


def test_429_surfaces_the_retry_hint_and_stays_retryable():
    from oto_mcp import error_taxonomy

    err = S.translate(_http(429, {"policyName": "TEN_SECONDLY_ROLLING"}))
    assert "retry" in str(err) and err.retryable is True
    info = error_taxonomy.classify(err)
    assert info.code == "rate_limited" and info.retryable
    daily = S.translate(_http(429, {"policyName": "DAILY"}))
    assert daily.retryable is False and "DAILY" in str(daily)


def test_401_keeps_the_token_guidance_and_still_marks_the_key():
    from oto_mcp import error_taxonomy

    err = S.translate(_http(401, {"category": "EXPIRED_AUTHENTICATION"}))
    assert "access token" in str(err).lower() and "refresh token" in str(err)
    assert error_taxonomy.credential_rejected_in_chain(err)


def test_other_refusals_are_left_alone():
    assert S.translate(_http(400, {"category": "VALIDATION_ERROR"})) is None
    assert S.translate(_http(403, {"category": "BANNED"})) is None


def test_the_error_envelope_relays_the_detail():
    from oto_mcp.middleware.error_envelope import _detail

    err = S.translate(_http(403, MISSING_TICKETS), object_type="tickets")
    try:
        raise err
    except McpError as e:
        assert _detail(e)["scope_to_add"] == "crm.objects.tickets.read"
    assert _detail(ValueError("x")) == {}


# --- les outils --------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    import oto.tools.hubspot.client as hs

    inst = MagicMock()
    monkeypatch.setattr(hs, "HubSpotClient", lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    return inst


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot as H, hubspot_pipelines as P

    m = FastMCP("t")
    H.register(m)
    P.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _pipelines(client, refus=None):
    def _req(method, path, **kw):
        if refus is not None:
            raise refus
        if path == "/crm/v3/pipelines/deals":
            return copy.deepcopy(DEAL_PIPELINES)
        if path == "/crm/v3/pipelines/tickets":
            return copy.deepcopy(TICKET_PIPELINES)
        if path == "/crm/v3/pipelines/deals/77":
            return copy.deepcopy(DEAL_PIPELINES["results"][1])
        raise AssertionError(path)
    client._request.side_effect = _req


def test_pipeline_list_deals_is_compact_ordered_and_keeps_archived_stages(client):
    _pipelines(client)
    out = _tool("hubspot_pipeline")(op="list", object_type="deals")
    sales = out["results"][0]
    assert [s["id"] for s in sales["stages"]] == ["appointmentscheduled", "old", "closedlost"]
    old = sales["stages"][1]
    assert old["archived"] is True and old["metadata"] == {"probability": "0.5"}
    assert sales["stages"][2]["metadata"] == {"probability": "0.0", "isClosed": "true"}
    assert "createdAt" not in sales and "createdAt" not in sales["stages"][2]
    assert out["results"][1]["label"] == "Partners"


def test_pipeline_list_tickets_serves_ticket_state(client):
    _pipelines(client)
    out = _tool("hubspot_pipeline")(op="list", object_type="tickets")
    assert out["results"][0]["stages"][1]["metadata"] == {"ticketState": "CLOSED"}


def test_pipeline_get_and_verbose(client):
    _pipelines(client)
    assert _tool("hubspot_pipeline")(op="get", object_type="deals",
                                     pipeline_id="77")["stages"][0]["label"] == "Signed"
    raw = _tool("hubspot_pipeline")(op="list", object_type="deals", verbose=True)
    assert raw["results"][0]["stages"][0]["createdAt"] == "x"
    with pytest.raises(McpError, match="pipeline_id"):
        _tool("hubspot_pipeline")(op="get", object_type="deals")


def test_pipeline_tool_refuses_with_the_scope(client):
    _pipelines(client, refus=_http(403, MISSING_SANS_NOM))
    with pytest.raises(McpError) as e:
        _tool("hubspot_pipeline")(op="list", object_type="tickets")
    assert "`tickets`" in e.value.error.message


def test_pipeline_tool_is_declared_read_only():
    from fastmcp import FastMCP
    from oto_mcp.tools import hubspot_pipelines as P
    from oto_mcp.tools.lecture import en_lecture

    m = FastMCP("t")
    P.register(m)
    assert en_lecture(asyncio.run(m.get_tool("hubspot_pipeline")))


DEALS_PAGE = {"results": [
    {"id": "1", "properties": {"dealname": "A", "dealstage": "closedlost",
                               "pipeline": "default"}},
    {"id": "2", "properties": {"dealname": "B", "dealstage": "123456", "pipeline": "77"}},
], "paging": {"next": {"after": "2"}}}


def test_without_resolve_labels_hubspot_object_returns_exactly_what_it_did(client):
    """Régression : sans le drapeau, la réponse du client repart INTACTE — même
    objet, aucune clé ajoutée, aucun appel de plus."""
    page = copy.deepcopy(DEALS_PAGE)
    client.search_objects.return_value = page
    out = _tool("hubspot_object")(op="search", object_type="deals")
    assert out is page and out == DEALS_PAGE
    client._request.assert_not_called()


def test_resolve_labels_adds_siblings_and_keeps_raw_ids(client):
    _pipelines(client)
    client.list_objects.return_value = copy.deepcopy(DEALS_PAGE)
    out = _tool("hubspot_object")(op="list", object_type="deals", resolve_labels=True)
    p1, p2 = (r["properties"] for r in out["results"])
    assert p1["dealstage"] == "closedlost" and p1["dealstage_label"] == "Closed lost"
    assert p1["pipeline_label"] == "Sales"
    assert p2["dealstage_label"] == "Signed" and p2["pipeline_label"] == "Partners"
    assert client._request.call_count == 1, "les pipelines se lisent une fois par appel"


def test_resolve_labels_on_one_ticket(client):
    _pipelines(client)
    client.get_object.return_value = {
        "id": "9", "properties": {"hs_pipeline": "0", "hs_pipeline_stage": "4"}}
    out = _tool("hubspot_object")(op="get", object_type="tickets", object_id="9",
                                  resolve_labels=True)
    assert out["properties"]["hs_pipeline_stage_label"] == "Closed"
    assert out["properties"]["hs_pipeline_label"] == "Support"


def test_resolve_labels_without_the_pipelines_scope_degrades_to_a_warning(client):
    _pipelines(client, refus=_http(403, MISSING_SANS_NOM))
    client.search_objects.return_value = copy.deepcopy(DEALS_PAGE)
    out = _tool("hubspot_object")(op="search", object_type="deals", resolve_labels=True)
    assert out["results"] == DEALS_PAGE["results"], "les objets, sans étiquette"
    assert "crm.objects.deals.read" in out["warnings"][0]


def test_hubspot_object_404_html_is_clean(client):
    client.get_object.side_effect = _http(404, HTML_404)
    with pytest.raises(Exception) as e:
        _tool("hubspot_object")(op="get", object_type="tickets", object_id="42")
    assert "<html" not in str(e.value) and "not found" in str(e.value)


def test_empty_results_are_not_an_error(client):
    client.search_objects.return_value = {"results": []}
    assert _tool("hubspot_object")(op="search", object_type="tickets") == {"results": []}


# --- hubspot_property --------------------------------------------------------------

PROPS = {"results": [
    {"name": "dealname", "label": "Deal name", "type": "string", "fieldType": "text",
     "groupName": "dealinformation", "options": [], "hubspotDefined": True,
     "description": "long text " * 50},
    {"name": "dealstage", "label": "Deal stage", "type": "enumeration",
     "fieldType": "select", "groupName": "dealinformation", "options": [],
     "hubspotDefined": True},
    {"name": "pipeline", "label": "Pipeline", "type": "enumeration",
     "fieldType": "select", "groupName": "dealinformation", "options": [],
     "hubspotDefined": True},
    {"name": "hs_internal_x", "label": "Internal", "type": "string", "fieldType": "text",
     "groupName": "dealinformation", "hidden": True, "hubspotDefined": True},
    {"name": "churn_risk", "label": "Churn risk", "type": "enumeration",
     "fieldType": "select", "groupName": "custom",
     "options": [{"value": "hi", "label": "High", "displayOrder": 0, "hidden": False}]},
]}


def test_property_list_is_compact_hides_internal_and_fills_stage_options(client):
    _pipelines(client)
    client.list_properties.return_value = copy.deepcopy(PROPS)
    out = _tool("hubspot_property")(op="list", object_type="deals")
    names = [p["name"] for p in out["results"]]
    assert "hs_internal_x" not in names
    assert out["projection"]["dropped"] == {"hidden": 1}
    assert set(out["results"][0]) == {"name", "label", "type", "fieldType",
                                      "groupName", "options"}
    stage = next(p for p in out["results"] if p["name"] == "dealstage")
    assert {"value": "closedlost", "label": "Closed lost"} in [
        {k: o[k] for k in ("value", "label")} for o in stage["options"]]
    grouped = {g["pipeline_label"]: g for g in stage["pipeline_options"]}
    assert set(grouped) == {"Sales", "Partners"}
    assert [o["value"] for o in grouped["Partners"]["options"]] == ["123456"]
    pipeline = next(p for p in out["results"] if p["name"] == "pipeline")
    assert [o["value"] for o in pipeline["options"]] == ["default", "77"]
    churn = next(p for p in out["results"] if p["name"] == "churn_risk")
    assert churn["options"] == [{"value": "hi", "label": "High"}]


def test_property_list_filters(client):
    _pipelines(client)
    client.list_properties.return_value = copy.deepcopy(PROPS)
    tool = _tool("hubspot_property")
    out = tool(op="list", object_type="deals", custom_only=True)
    assert [p["name"] for p in out["results"]] == ["churn_risk"]
    out = tool(op="list", object_type="deals", names=["dealname", "nope"])
    assert [p["name"] for p in out["results"]] == ["dealname"]
    assert out["unknown_names"] == ["nope"]
    out = tool(op="list", object_type="deals", group="custom")
    assert [p["name"] for p in out["results"]] == ["churn_risk"]
    out = tool(op="list", object_type="deals", include_hidden=True, verbose=True)
    assert "hs_internal_x" in [p["name"] for p in out["results"]]
    assert out["results"][0]["description"].startswith("long text")


def test_property_list_without_pipelines_scope_still_answers(client):
    _pipelines(client, refus=_http(403, MISSING_SANS_NOM))
    client.list_properties.return_value = copy.deepcopy(PROPS)
    out = _tool("hubspot_property")(op="list", object_type="deals")
    stage = next(p for p in out["results"] if p["name"] == "dealstage")
    assert stage["options"] == [] and "pipelines" in stage["warnings"][0]


def test_property_list_on_contacts_never_reads_pipelines(client):
    client.list_properties.return_value = {"results": [
        {"name": "email", "label": "Email", "type": "string", "fieldType": "text",
         "groupName": "contactinformation"}]}
    _tool("hubspot_property")(op="list", object_type="contacts")
    client._request.assert_not_called()
