"""Amplitude connector, read only — the five `amplitude_*` tools and the probe.

Generic tripwires cover the registry, publisher, logo, served prose and the
join to the oto-core client. This file pins what is SPECIFIC to the module,
through the real FastMCP path (`mcp.call_tool`), the client replaced by an
in-memory double:

- the credential's region picks the client, an unknown region is refused;
- the probe turns a 403 into the region / secret diagnosis (`NonAutorise`);
- the cost guard on the window and segments, events passed by name;
- each op refuses the arguments it does not use;
- slim views, bounded previews, the async cohort flow.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS

from oto_mcp import access
from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import amplitude as A
from oto_mcp.tools import amplitude_query as Q


def _err(status, details):
    from oto.tools.common import UpstreamHTTPError
    return UpstreamHTTPError(status, {"error": {"http_code": status, "type": "unspecified",
                                                "metadata": {"details": details}}},
                             service="amplitude")


class _FauxClient:
    def __init__(self, api_key, secret_key, region="us"):
        self.api_key, self.secret_key, self.region = api_key, secret_key, region
        self.appels = []
        self.leve = None
        self.status = "JOB COMPLETED"

    def _note(self, nom, *args, **kw):
        self.appels.append((nom, args, kw))
        if self.leve:
            raise self.leve

    def list_event_types(self):
        self._note("list_event_types")
        return {"success": True, "data": [
            {"event_type": "Sign Up", "category": {"name": "Onboarding"},
             "description": "Account created", "is_active": True},
            {"event_type": "Purchase", "category": None, "description": "",
             "is_hidden": True}]}

    def list_event_properties(self, event_type=None):
        self._note("list_event_properties", event_type)
        return {"data": [{"event_property": "plan", "type": "string"}]}

    def list_user_properties(self):
        self._note("list_user_properties")
        return {"data": [{"user_property": "gp:company", "type": "string"}]}

    def list_group_properties(self):
        self._note("list_group_properties")
        return {"data": []}

    def list_events(self):
        self._note("list_events")
        return {"data": [{"value": "Sign Up", "totals": 42}]}

    def segmentation(self, event, start, end, **kw):
        self._note("segmentation", event, start, end, **kw)
        return {"data": {"series": [[1, 2]], "xValues": ["2026-09-01", "2026-09-02"]},
                "novaCost": 1, "realtimeProcessLag": {"p50": 1.0}, "novaRuntime": 410}

    def funnel(self, events, start, end, **kw):
        self._note("funnel", events, start, end, **kw)
        return {"data": [{"cumulative": [10, 4]}]}

    def retention(self, se, re, start, end, **kw):
        self._note("retention", se, re, start, end, **kw)
        return {"data": {}}

    def active_users(self, start, end, **kw):
        self._note("active_users", start, end, **kw)
        return {"data": {}}

    def sessions(self, kind, start, end):
        self._note("sessions", kind, start, end)
        return {"data": {}}

    def chart_query(self, chart_id):
        self._note("chart_query", chart_id)
        return {"data": {"series": []}}

    def chart_csv(self, chart_id):
        self._note("chart_csv", chart_id)
        return "h\n" + "\n".join(str(i) for i in range(150))

    def user_search(self, user):
        self._note("user_search", user)
        return {"matches": [{"amplitude_id": 7, "user_id": user}]}

    def user_activity(self, amplitude_id, **kw):
        self._note("user_activity", amplitude_id, **kw)
        return {"userData": {"user_id": "u1"}, "events": [
            {"event_type": "Sign Up", "event_time": "2026-09-01 10:00:00",
             "session_id": 1, "event_properties": {"plan": "pro"},
             "device_id": "d", "ip_address": "x"}]}

    def list_cohorts(self):
        self._note("list_cohorts")
        return {"cohorts": [{"id": "c1", "name": "Power users", "size": 12,
                             "lastComputed": 1, "definition": {"big": True}}]}

    def request_cohort(self, cohort_id, props=False):
        self._note("request_cohort", cohort_id, props=props)
        return {"request_id": "r1", "cohort_id": cohort_id}

    def cohort_status(self, request_id):
        self._note("cohort_status", request_id)
        return {"async_status": self.status}

    def cohort_file(self, request_id):
        self._note("cohort_file", request_id)
        return "amplitude_id,user_id\n1,a\n2,b\n"


class _Banc:
    prepare = None


@pytest.fixture
def banc(monkeypatch):
    b = _Banc()
    b.champs = {"api_key": "k", "secret_key": "s"}
    b.construits = []
    monkeypatch.setattr(access, "resolve_credential_fields",
                        lambda provider, account=None: dict(b.champs))
    import oto.tools.amplitude as pkg

    def _construire(*args, **kw):
        c = _FauxClient(*args, **kw)
        if b.prepare:
            b.prepare(c)
        b.construits.append(c)
        return c

    monkeypatch.setattr(pkg, "AmplitudeClient", _construire)
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    b.mcp = FastMCP("banc-amplitude")
    A.register(b.mcp)
    Q.register(b.mcp)
    return b


def _appeler(b, outil, **arguments):
    return asyncio.run(b.mcp.call_tool(outil, arguments)).structured_content


def _refus(b, outil, **arguments) -> McpError:
    fn = {t.name: t for t in asyncio.run(b.mcp._list_tools())}[outil].fn
    with pytest.raises(McpError) as e:
        fn(**arguments)
    assert e.value.error.code == INVALID_PARAMS
    return e.value


def test_cinq_outils_avec_description(banc):
    outils = asyncio.run(banc.mcp.list_tools(run_middleware=False))
    assert {t.name for t in outils} == {"amplitude_schema", "amplitude_chart",
                                        "amplitude_query", "amplitude_user",
                                        "amplitude_cohort"}
    assert all(t.description and len(t.description) > 80 for t in outils)


def test_provider_declare_les_deux_modules():
    from oto_mcp import providers
    c = providers.REGISTRY["amplitude"]
    assert c.modules == ("amplitude", "amplitude_query")
    assert [f.name for f in c.credential_fields] == ["api_key", "secret_key", "region"]


# --- region and probe ---------------------------------------------------------

def test_region_absente_vise_les_us(banc):
    _appeler(banc, "amplitude_schema")
    c = banc.construits[0]
    assert (c.api_key, c.secret_key, c.region) == ("k", "s", "us")


def test_region_eu(banc):
    banc.champs["region"] = "EU"
    _appeler(banc, "amplitude_schema")
    assert banc.construits[0].region == "eu"


def test_region_inconnue_refusee(banc):
    banc.champs["region"] = "asia"
    _refus(banc, "amplitude_schema")
    assert banc.construits == []


def test_sonde_enregistree_et_ok(banc):
    assert cv.supports("amplitude")
    A._verify({"api_key": "k", "secret_key": "s", "region": "us"})
    assert banc.construits[-1].appels == [("list_event_types", (), {})]


@pytest.mark.parametrize("details,attendu", [
    ("Invalid API/Secret Key combination", "secret key"),
    ("Invalid API Key", "« eu »"),
])
def test_sonde_nomme_la_cause(banc, details, attendu):
    banc.prepare = lambda c: setattr(c, "leve", _err(403, details))
    with pytest.raises(cv.NonAutorise) as e:
        A._verify({"api_key": "k", "secret_key": "s"})
    assert attendu in str(e.value)
    assert cv.classer(e.value) == cv.UNAUTHORIZED


def test_un_403_dans_un_outil_devient_un_refus(banc):
    banc.prepare = lambda c: setattr(c, "leve", _err(403, "Invalid API Key"))
    _refus(banc, "amplitude_schema")


def test_429_reste_type(banc):
    from oto.tools.common import UpstreamHTTPError
    banc.prepare = lambda c: setattr(c, "leve", UpstreamHTTPError(429, "slow", service="amplitude"))
    fn = {t.name: t for t in asyncio.run(banc.mcp._list_tools())}["amplitude_schema"].fn
    with pytest.raises(UpstreamHTTPError):
        fn()


# --- schema ------------------------------------------------------------------

def test_schema_events_vue_resserree_et_recherche(banc):
    r = _appeler(banc, "amplitude_schema", search="sign")
    assert r == {"op": "events", "count": 1, "items": [
        {"name": "Sign Up", "category": "Onboarding", "description": "Account created"}]}


def test_schema_volumes(banc):
    r = _appeler(banc, "amplitude_schema", op="volumes")
    assert r["items"] == [{"name": "Sign Up", "weekly_totals": 42}]


def test_schema_event_type_hors_event_properties_refuse(banc):
    _refus(banc, "amplitude_schema", op="events", event_type="Sign Up")


# --- query ------------------------------------------------------------------

def test_segmentation_evenement_par_nom(banc):
    r = _appeler(banc, "amplitude_query", op="segmentation", start="2026-09-01",
                 end="2026-09-30", event="Sign Up", interval="week", group_by="country")
    nom, args, kw = banc.construits[0].appels[0]
    assert nom == "segmentation"
    assert args == ({"event_type": "Sign Up"}, "20260901", "20260930")
    assert kw["interval"] == 7 and kw["group_by"] == "country"
    assert r["data"]["series"] == [[1, 2]]
    assert r["cost"] == 1 and "realtimeProcessLag" not in r and "novaRuntime" not in r


def test_funnel_etapes_et_fenetre_de_conversion(banc):
    _appeler(banc, "amplitude_query", op="funnel", start="2026-09-01", end="2026-09-30",
             events=["Sign Up", {"event_type": "Purchase"}], conversion_window_days=7)
    nom, args, kw = banc.construits[0].appels[0]
    assert args[0] == [{"event_type": "Sign Up"}, {"event_type": "Purchase"}]
    assert kw["conversion_window_seconds"] == 7 * 86400


def test_funnel_une_seule_etape_refuse(banc):
    _refus(banc, "amplitude_query", op="funnel", start="2026-09-01", end="2026-09-30",
           events=["Sign Up"])


def test_retention_retour_par_defaut_active(banc):
    _appeler(banc, "amplitude_query", op="retention", start="2026-09-01",
             end="2026-09-30", event="Sign Up")
    _, args, _ = banc.construits[0].appels[0]
    assert args[1] == {"event_type": "_active"}


def test_argument_inutile_refuse(banc):
    _refus(banc, "amplitude_query", op="segmentation", start="2026-09-01",
           end="2026-09-30", event="Sign Up", events=["A", "B"])
    _refus(banc, "amplitude_query", op="sessions", start="2026-09-01",
           end="2026-09-30", event="Sign Up")


def test_fenetre_longue_exige_long_range(banc):
    _refus(banc, "amplitude_query", op="active_users", start="2026-01-01", end="2026-09-30")
    _appeler(banc, "amplitude_query", op="active_users", start="2026-01-01",
             end="2026-09-30", long_range=True)


def test_fenetre_plafonnee_et_ordonnee(banc):
    _refus(banc, "amplitude_query", op="active_users", start="2024-01-01",
           end="2026-09-30", long_range=True)
    _refus(banc, "amplitude_query", op="active_users", start="2026-09-30", end="2026-09-01")
    _refus(banc, "amplitude_query", op="active_users", start="last week", end="2026-09-01")


def test_trop_de_segments(banc):
    _refus(banc, "amplitude_query", op="active_users", start="2026-09-01",
           end="2026-09-30", segments=[{"prop": "country", "op": "is",
                                        "values": [str(i)]} for i in range(6)])
    assert banc.construits == []


# --- chart, user, cohort -------------------------------------------------------

def test_chart_json_et_csv_borne(banc):
    assert _appeler(banc, "amplitude_chart", chart_id="abc")["data"] == {"series": []}
    r = _appeler(banc, "amplitude_chart", chart_id="abc", format="csv")
    assert r["total_lines"] == 151 and r["truncated"] and len(r["lines"]) == A.PREVIEW_LINES


def test_user_search_puis_activity(banc):
    assert _appeler(banc, "amplitude_user", user="u1")["matches"][0]["amplitude_id"] == 7
    r = _appeler(banc, "amplitude_user", op="activity", amplitude_id="7", limit=5000)
    assert r["events"] == [{"event_type": "Sign Up", "event_time": "2026-09-01 10:00:00",
                            "session_id": 1, "event_properties": {"plan": "pro"}}]
    assert banc.construits[-1].appels[0][2] == {"limit": 1000}


def test_user_activity_sans_id_refuse(banc):
    _refus(banc, "amplitude_user", op="activity", user="u1")


def test_cohort_list_resserree(banc):
    r = _appeler(banc, "amplitude_cohort")
    assert r == {"cohorts": [{"id": "c1", "name": "Power users", "size": 12,
                              "last_computed": 1}]}


def test_cohort_request_termine_rend_un_apercu(banc):
    r = _appeler(banc, "amplitude_cohort", op="request", cohort_id="c1")
    assert r["status"] == "completed" and r["total_lines"] == 3
    assert r["lines"][0] == "amplitude_id,user_id"


def test_cohort_request_en_cours_rend_le_request_id(banc, monkeypatch):
    banc.prepare = lambda c: setattr(c, "status", "JOB INPROGRESS")
    monkeypatch.setattr(A, "COHORT_WAIT_S", 0)
    r = _appeler(banc, "amplitude_cohort", op="request", cohort_id="c1")
    assert r["status"] == "running" and r["request_id"] == "r1"


def test_cohort_members(banc):
    r = _appeler(banc, "amplitude_cohort", op="members", request_id="r1")
    assert r["status"] == "completed" and r["lines"][1] == "1,a"
    _refus(banc, "amplitude_cohort", op="members", request_id="r1", cohort_id="c1")


def test_400_propriete_inconnue_renvoie_au_schema(banc):
    banc.prepare = lambda c: setattr(c, "leve", _err(400, "Invalid user property country"))
    e = _refus(banc, "amplitude_query", op="segmentation", start="2026-09-01",
               end="2026-09-07", event="Sign Up", group_by="country")
    assert "amplitude_schema" in e.error.message
