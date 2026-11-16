"""Surface REST des lentilles monitoring, exercée par l'ADAPTATEUR réel.

Le risque de la migration routes-écrites-main → capacités n'est pas le handler
(testé ailleurs) mais le TRANSPORT : les filtres arrivent en query string, donc en
`str`, et doivent être coercés par l'Input pydantic (`errors=1` → bool,
`min_duration_ms=5000` → int). Un test qui appellerait le handler avec des types
Python déjà propres déclarerait vert un chemin cassé — on monte donc les vraies
routes via `_rest_adapter.make_routes` et on tape dessus.
"""
import asyncio

import pytest
from starlette.datastructures import QueryParams

from oto_mcp import db
from oto_mcp.capabilities import _authz, _rest_adapter
from oto_mcp.capabilities.registry import CAPABILITIES


class FakeReq:
    """Le minimum qu'une Request Starlette expose à l'adaptateur — `headers` et
    `client` compris : il y lit l'empreinte du client (IP réelle, user-agent) pour
    la poser autour du handler (#487)."""

    def __init__(self, query=None, path_params=None, method="GET"):
        # `QueryParams`, pas un dict : l'adaptateur lit `getlist` (clés répétées, #418).
        self.query_params = QueryParams(query or {})
        self.path_params = path_params or {}
        self.method = method
        self.headers = {}
        self.client = None
        # Le `scope` ASGI : l'adaptateur y lit le principal publié par
        # l'authentification, et y dépose le geste de la requête (oto#273).
        self.scope = {}

    async def json(self):
        return {}


def _mount(monkeypatch, *, admin=True):
    """Routes des seules capacités monitoring. On garde la VRAIE règle d'autz
    (PLATFORM_ADMIN) et on stubbe l'opérateur plateforme dessous — la capacité est
    frozen, et un stub de règle masquerait un palier mal déclaré."""
    monkeypatch.setattr(_authz.access, "is_platform_operator", lambda s: admin)
    # La règle enrichit le ctx (org active + rôle) → sans DB dans ce test.
    monkeypatch.setattr(_authz.access, "current_org", lambda s: None)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda s: "admin")

    async def authenticate(request, verifier):
        return ("admin-sub", None)

    def json_response(request, payload, status=200):
        return payload

    def json_error(request, status, code, detail=None):
        return {"_status": status, "_code": code, "_detail": detail}

    async def options_handler(request):
        return "opt"

    caps = [c for c in CAPABILITIES if c.key.startswith("monitoring.")]
    routes = _rest_adapter.make_routes(
        None, authenticate, json_response, json_error, options_handler, caps)
    return {(r.path, m): r.endpoint for r in routes for m in r.methods}


def test_non_admin_is_refused(monkeypatch):
    """Le palier ne s'est pas perdu dans la migration : sans opérateur → 403."""
    monkeypatch.setattr(db, "list_tool_calls_page", lambda **kw: {"calls": [], "next": None})
    idx = _mount(monkeypatch, admin=False)
    out = asyncio.run(idx[("/api/admin/monitoring/calls", "GET")](FakeReq()))
    assert out["_status"] == 403


def test_query_string_filters_are_coerced(monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "list_tool_calls_page",
                        lambda **kw: seen.update(kw) or {"calls": [], "next": None})
    idx = _mount(monkeypatch)
    handler = idx[("/api/admin/monitoring/calls", "GET")]

    asyncio.run(handler(FakeReq(query={
        "days": "7", "limit": "50", "errors": "1",
        "min_duration_ms": "5000", "error_contains": "timeout", "tool": "folk_record",
    })))

    # Les valeurs arrivent typées, pas en str — c'est ce que le SQL attend.
    assert seen["since_days"] == 7 and isinstance(seen["since_days"], int)
    assert seen["limit"] == 50 and isinstance(seen["limit"], int)
    assert seen["errors_only"] is True
    assert seen["min_duration_ms"] == 5000 and isinstance(seen["min_duration_ms"], int)
    assert seen["error_contains"] == "timeout"


def test_call_detail_route_reads_path_param(monkeypatch):
    monkeypatch.setattr(db, "get_tool_call", lambda cid: {"id": cid, "tool": "fr_get"})
    idx = _mount(monkeypatch)
    handler = idx[("/api/admin/monitoring/calls/{call_id}", "GET")]
    out = asyncio.run(handler(FakeReq(path_params={"call_id": "42"})))
    assert out == {"call": {"id": 42, "tool": "fr_get"}}


def test_unknown_call_is_a_404_not_a_500(monkeypatch):
    monkeypatch.setattr(db, "get_tool_call", lambda cid: None)
    idx = _mount(monkeypatch)
    handler = idx[("/api/admin/monitoring/calls/{call_id}", "GET")]
    out = asyncio.run(handler(FakeReq(path_params={"call_id": "999"})))
    assert out["_status"] == 404 and out["_code"] == "unknown_call"


@pytest.mark.parametrize("path,fn,key,days", [
    # Sans périmètre, le résumé couvre toute la plateforme : 7 jours au plus (#1145).
    ("/api/admin/monitoring/summary", "tool_call_stats", "since_days", 7),
    ("/api/admin/monitoring/rest", "rest_call_stats", "since_days", 30),
    ("/api/admin/monitoring/connectors", "connector_failure_stats", "since_days", 30),
])
def test_windowed_lenses_pass_days(monkeypatch, path, fn, key, days):
    seen = {}
    monkeypatch.setattr(db, fn, lambda **kw: seen.update(kw) or {})
    idx = _mount(monkeypatch)
    asyncio.run(idx[(path, "GET")](FakeReq(query={"days": str(days)})))
    assert seen[key] == days


def test_le_resume_plateforme_sans_perimetre_est_borne(monkeypatch):
    """#1145 : sans `org_id` ni `sub`, le résumé lit le journal de toute la plateforme
    (452 s pour un jour, le 04/10/2026, sous contention). Au-delà de la borne, refus
    NOMMÉ qui dit quoi passer ; avec un périmètre, la fenêtre s'élargit."""
    seen = {}
    monkeypatch.setattr(db, "tool_call_stats", lambda **kw: seen.update(kw) or {})
    idx = _mount(monkeypatch)
    out = asyncio.run(idx[("/api/admin/monitoring/summary", "GET")](
        FakeReq(query={"days": "30"})))
    assert out["_status"] == 400 and out["_code"] == "days_too_large"
    assert not seen
    asyncio.run(idx[("/api/admin/monitoring/summary", "GET")](
        FakeReq(query={"days": "30", "org_id": "7"})))
    assert seen["since_days"] == 30 and seen["org_id"] == 7
    out = asyncio.run(idx[("/api/admin/monitoring/summary", "GET")](
        FakeReq(query={"days": "91", "org_id": "7"})))
    assert out["_status"] == 400 and out["_code"] == "days_too_large"


def test_funnel_defaults_to_30_days(monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "activation_funnel", lambda **kw: seen.update(kw) or {})
    idx = _mount(monkeypatch)
    asyncio.run(idx[("/api/admin/monitoring/funnel", "GET")](FakeReq()))
    assert seen["active_window_days"] == 30


def test_la_lentille_rest_scope_par_compte_depuis_la_query_string(monkeypatch):
    """#451 : la lentille REST accepte désormais `sub`/`org_id` (elle les jetait).
    Ces valeurs arrivent en query string, donc en `str` : `org_id` doit atteindre le
    SQL en `int`, sinon PG compare une chaîne à un bigint et la lecture échoue —
    un test qui appellerait le handler avec des types propres ne le verrait pas."""
    from oto_mcp.capabilities import monitoring

    seen = {}
    monkeypatch.setattr(db, "rest_call_stats", lambda **kw: seen.update(kw) or {})
    monkeypatch.setattr(monitoring, "_resolve_sub", lambda t: "sub-jane" if t else None)
    idx = _mount(monkeypatch)
    asyncio.run(idx[("/api/admin/monitoring/rest", "GET")](
        FakeReq(query={"days": "30", "org_id": "42", "sub": "jane@example.com"})))

    assert seen["org_id"] == 42 and isinstance(seen["org_id"], int)
    assert seen["sub"] == "sub-jane"
