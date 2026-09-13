"""Connecteur Monid — la passerelle payante vers les endpoints de ~70 fournisseurs.

Les outils appellent le VRAI client d'oto-core ; seul `requests.Session.request` est
remplacé par un faux transport qui rejoue le contrat OpenAPI 0.1.0, et l'horloge (celle
du client ET celle de l'outil) est une horloge factice : les attentes se mesurent sans
dormir. Ce banc verrouille ce que le connecteur ajoute au contrat :

- le registre : clé d'API, BYO par défaut, clé plateforme sur grant seulement ;
- ADR 0047 : un argument hors de son `op` est refusé avant toute résolution de clé ;
- le lancement : l'enveloppe par statut (`provider_ok`, `cost_usd`, `next_step`), un run
  rendu sous le code HTTP du fournisseur, l'attente bornée DE BOUT EN BOUT, une relecture
  qui échoue rendue comme un run et pas comme une erreur ;
- l'issue inconnue : refusée (`INTERNAL_ERROR`, jamais « argument invalide ») en nommant
  la liste des runs — ou un administrateur sous la clé plateforme —, jamais re-tentée ;
- la clé plateforme : workspace partagé, sa liste de runs et son solde refusés,
  `get`/`stop` par id ;
- le métrage : 1 par run rendu, la clé plateforme seule débite le quota, et un débit qui
  échoue ne cache pas le run ;
- la traduction des refus par CODE, 429/5xx laissés réessayables à la taxonomie.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from urllib.parse import urlsplit

import psycopg
import psycopg_pool
import pytest
import requests
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS
from oto.tools.monid.client import MonidHTTPError

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.error_taxonomy import _is_expected_error, classify
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of

OUTILS = {"monid_endpoint", "monid_run", "monid_runs", "monid_wallet"}
CLE = "monid_live_banc"
RID = "01JBANC0000000000000000000"
PROVIDER, ENDPOINT = "acme-data", "/v1/lookup"


# --- faux transport, horloge factice ----------------------------------------------

class Resp:
    def __init__(self, status: int, body=None, *, headers=None, raw: str | None = None):
        self.status_code = status
        self.text = raw if raw is not None else ("" if body is None else _json.dumps(body))
        self.content = self.text.encode()
        self.headers = {"Content-Type": "text/html" if raw is not None else "application/json",
                        "x-request-id": "req-banc", **(headers or {})}

    def json(self):
        return _json.loads(self.text)


class Horloge:
    """`time` du client et de l'outil : `sleep` avance l'horloge au lieu de dormir."""

    def __init__(self):
        self.t = 1000.0
        self.sommeils: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sommeils.append(s)
        self.t += s


class FakeMonid:
    """Chaque route tient une file de réponses (la dernière se répète) ; une exception
    dans la file est levée par le transport. `latence` avance l'horloge par appel."""

    def __init__(self, horloge: Horloge):
        self.horloge = horloge
        self.log: list[dict] = []
        self.routes: dict[tuple[str, str], list] = {}
        self.latence: dict[str, float] = {}

    def __call__(self, session, method, url, params=None, json=None, timeout=None, **kw):
        path = urlsplit(url).path
        self.log.append({"method": method, "path": path, "params": params, "json": json,
                         "timeout": timeout, "auth": session.headers.get("Authorization"),
                         "redirects": kw.get("allow_redirects")})
        self.horloge.t += self.latence.get(method, 0.0)
        file = self.routes.get((method, path))
        if not file:
            return Resp(404, {"code": 404, "message": "Not found"})
        item = file.pop(0) if len(file) > 1 else file[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def envois(self, method=None, path=None):
        return [e for e in self.log if (method is None or e["method"] == method)
                and (path is None or e["path"] == path)]


def _run(status="COMPLETED", http=200, **extra):
    run = {"runId": RID, "provider": PROVIDER, "endpoint": ENDPOINT, "status": status,
           "price": {"type": "PER_CALL", "amount": {"value": 0.1, "currency": "USD"}},
           "createdAt": "2026-09-12T10:00:00Z"}
    if status == "COMPLETED":
        run.update(providerResponse={"httpStatus": http}, output={"email": "x@example.test"},
                   billing={"reportedCost": {"value": 100000, "unit": "MICRO_DOLLAR",
                                             "currency": "USD"}})
    run.update(extra)
    return run


@pytest.fixture()
def horloge(monkeypatch):
    h = Horloge()
    monkeypatch.setattr("oto.tools.monid.client.time", h)
    monkeypatch.setattr("oto_mcp.tools.monid.time", h)
    return h


@pytest.fixture()
def fake(monkeypatch, horloge):
    srv = FakeMonid(horloge)
    monkeypatch.setattr("requests.Session.request",
                        lambda session, method, url, **kw: srv(session, method, url, **kw))
    return srv


@pytest.fixture()
def cle(monkeypatch):
    """La clé que résout le coffre ; `platform` dit si c'est celle de la plateforme."""
    box = {"key": CLE, "platform": False, "resolutions": 0}

    def _resolve(provider, account=None):
        assert provider == "monid"
        box["resolutions"] += 1
        return box["key"], box["platform"]

    monkeypatch.setattr("oto_mcp.access.resolve_api_key", _resolve)
    return box


@pytest.fixture(autouse=True)
def metrage(monkeypatch):
    rec = {"trace": [], "usage": []}
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace",
                        lambda **kw: rec["trace"].append(kw))
    monkeypatch.setattr("oto_mcp.access.record_platform_usage",
                        lambda provider, calls=1: rec["usage"].append((provider, calls)))
    return rec


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _outil(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import monid

    m = FastMCP("t")
    monid.register(m)
    return asyncio.run(m.get_tool(name)).fn


# --- registre et surface ------------------------------------------------------------

def test_monid_is_a_keyed_connector_with_a_grant_only_platform_key():
    c = providers.REGISTRY["monid"]
    assert c.kind == "tools" and c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    assert c.platform_key_open is False and c.default_quota == 0
    assert "monid" in providers.KEY_PROVIDERS
    assert c.publisher_name == "Monid" and c.category == "Prospection"
    assert [f.name for f in c.secret_fields] == ["key"] and c.secret_fields[0].secret
    assert c.default_active is False
    # `help` est servi dans la carte des namespaces de TOUTES les sessions
    assert len(c.help) <= 140


def test_monid_has_its_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["monid"].doc_sections}
    assert {"prerequisite", "usage", "note"} <= kinds


def test_the_namespace_serves_exactly_four_tools(all_tools):
    got = {n for n in all_tools if namespace_of(n) == "monid"}
    assert got == OUTILS
    assert all((all_tools[n].description or "").strip() for n in got)


def test_the_verify_probe_is_registered(all_tools):
    assert connector_verify.supports("monid")
    assert connector_verify.couverture("monid") == connector_verify.AUTH


def test_defaults_are_reads_and_the_run_has_no_dry_run(all_tools):
    props = {n: all_tools[n].parameters["properties"] for n in OUTILS}
    assert props["monid_endpoint"]["op"]["default"] == "discover"
    assert props["monid_runs"]["op"]["default"] == "list"
    assert "dry_run" not in props["monid_run"]
    assert props["monid_run"]["wait_seconds"]["default"] == 20
    assert "_run_id" in props["monid_runs"]["run_id"]["description"]


# --- ADR 0047 : ce qui ne s'applique pas est refusé, avant la clé --------------------

@pytest.mark.parametrize("name, kwargs", [
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER, "endpoint": ENDPOINT, "q": "x"}),
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER, "endpoint": ENDPOINT, "limit": 5}),
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER, "endpoint": ENDPOINT, "full": True}),
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER, "endpoint": ENDPOINT,
                        "category": "lead-generation"}),
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER, "endpoint": ENDPOINT, "min_score": 0.5}),
    # `0 == False` : un zéro passé hors de son op se refuse comme toute autre valeur
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER, "endpoint": ENDPOINT, "min_score": 0}),
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER, "endpoint": ENDPOINT, "min_score": 0.0}),
    ("monid_endpoint", {"op": "inspect", "provider": PROVIDER}),
    ("monid_endpoint", {"q": "emails", "provider": PROVIDER}),
    ("monid_endpoint", {"q": "emails", "endpoint": ENDPOINT}),
    ("monid_endpoint", {}),
    ("monid_endpoint", {"q": "emails", "limit": 41}),
    ("monid_endpoint", {"q": "emails", "limit": 0}),
    ("monid_endpoint", {"op": "run", "q": "emails"}),
    ("monid_runs", {"run_id": RID}),
    ("monid_runs", {"wait_seconds": 5}),
    ("monid_runs", {"limit": 101}),
    ("monid_runs", {"op": "get"}),
    ("monid_runs", {"op": "get", "run_id": RID, "status": "RUNNING"}),
    ("monid_runs", {"op": "get", "run_id": RID, "limit": 5}),
    ("monid_runs", {"op": "get", "run_id": RID, "cursor": "c"}),
    ("monid_runs", {"op": "get", "run_id": RID, "full": True}),
    ("monid_runs", {"op": "get", "run_id": RID, "wait_seconds": 41}),
    ("monid_runs", {"op": "stop", "run_id": RID, "wait_seconds": 5}),
    ("monid_runs", {"op": "stop", "run_id": RID, "full": True}),
    ("monid_runs", {"op": "stop", "run_id": RID, "limit": 5}),
    ("monid_runs", {"op": "stop", "run_id": RID, "cursor": "c"}),
    ("monid_runs", {"op": "stop", "run_id": RID, "status": "RUNNING"}),
    ("monid_runs", {"op": "delete", "run_id": RID}),
    ("monid_run", {"provider": PROVIDER, "endpoint": ENDPOINT, "wait_seconds": 41}),
    ("monid_run", {"provider": PROVIDER, "endpoint": ENDPOINT, "wait_seconds": -1}),
])
def test_an_argument_that_does_not_apply_is_refused_before_the_key(fake, cle, name, kwargs):
    with pytest.raises(McpError) as e:
        _outil(name)(**kwargs)
    assert e.value.error.code == INVALID_PARAMS
    assert fake.log == [] and cle["resolutions"] == 0


def test_a_local_client_refusal_sends_nothing(fake, cle):
    with pytest.raises(McpError) as e:
        _outil("monid_endpoint")(q="emails", category="Not A Slug")
    assert e.value.error.code == INVALID_PARAMS and fake.log == []


def test_the_irrelevant_argument_guard_bites(fake, cle, monkeypatch):
    """Sans `_hors_op`, un `provider` passé à discover serait avalé en silence."""
    from oto_mcp.tools import monid
    fake.routes[("POST", "/v1/discover/endpoints")] = [Resp(200, {"items": [], "total": 0})]
    monkeypatch.setattr(monid, "_hors_op", lambda op, **kw: None)
    assert _outil("monid_endpoint")(q="emails", provider=PROVIDER)["total"] == 0


# --- catalogue ----------------------------------------------------------------------

def test_discover_sends_q_and_min_score_and_names_what_it_drops(fake, cle):
    carte = {"provider": PROVIDER, "providerDisplayName": "Acme", "endpoint": ENDPOINT,
             "displayName": "Lookup", "displayDescription": "", "price": {}, "tags": [],
             "categories": [], "providerDisplayDescription": "long", "supportedX402Networks": []}
    fake.routes[("POST", "/v1/discover/endpoints")] = [Resp(200, {"items": [carte], "total": 7})]
    page = _outil("monid_endpoint")(q="  work email  ", limit=5, min_score=0.5)
    envoi = fake.log[-1]
    assert envoi["json"] == {"q": "work email", "limit": 5, "minScore": 0.5}
    assert envoi["auth"] == f"Bearer {CLE}" and CLE not in _json.dumps(envoi["params"])
    assert page["total"] == 7
    assert "providerDisplayDescription" not in page["items"][0]
    assert page["projection"]["omitted"] == ["providerDisplayDescription", "supportedX402Networks"]
    brut = _outil("monid_endpoint")(q="work email", full=True)
    assert brut["items"][0]["providerDisplayDescription"] == "long" and "projection" not in brut


def test_inspect_passes_provider_and_endpoint_verbatim_and_returns_the_card(fake, cle):
    carte = {"provider": "api.acme.io", "endpoint": ENDPOINT, "input": {"body": {}},
             "notes": ["n"]}
    fake.routes[("POST", "/v1/inspect")] = [Resp(200, carte)]
    out = _outil("monid_endpoint")(op="inspect", provider="api.acme.io", endpoint=ENDPOINT)
    assert out == carte
    assert fake.log[-1]["json"] == {"provider": "api.acme.io", "endpoint": ENDPOINT}


# --- le lancement ---------------------------------------------------------------------

def test_a_completed_run_is_ready_to_read(fake, cle, metrage):
    fake.routes[("POST", "/v1/run")] = [Resp(200, _run())]
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT,
                              query_params={"url": "https://example.test/in/x"})
    assert out["done"] is True and out["provider_ok"] is True and out["next_step"] is None
    assert out["cost_usd"] == pytest.approx(0.1) and out["run"]["runId"] == RID
    (envoi,) = fake.log
    assert envoi["json"] == {"provider": PROVIDER, "endpoint": ENDPOINT,
                             "input": {"queryParams": {"url": "https://example.test/in/x"}}}
    assert envoi["timeout"] == (10, 35) and envoi["redirects"] is False
    assert metrage["trace"] == [{"quantity": 1}] and metrage["usage"] == []


def test_a_provider_404_comes_back_as_a_run_not_an_error(fake, cle):
    fake.routes[("POST", "/v1/run")] = [Resp(404, _run(http=404, providerResponse={
        "httpStatus": 404, "error": {"message": "no match"}}))]
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["done"] is True and out["provider_ok"] is False
    assert "404" in out["next_step"] and "no match" in out["next_step"]


@pytest.mark.parametrize("reponse", [{}, {"httpStatus": True}, {"httpStatus": "200"}])
def test_a_completed_run_without_a_provider_status_says_so(fake, cle, reponse):
    """`provider_ok` n'est pas `null` que pendant l'exécution : sans statut HTTP lisible
    du fournisseur (un booléen n'en est pas un), il reste vide et `next_step` le dit."""
    fake.routes[("POST", "/v1/run")] = [Resp(200, _run(providerResponse=reponse))]
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["done"] is True and out["provider_ok"] is None
    assert out["next_step"] and "statut HTTP" in out["next_step"]


def test_a_waited_run_that_completes_without_a_provider_response(fake, cle):
    fini = _run()
    del fini["providerResponse"]
    fake.routes[("POST", "/v1/run")] = [Resp(202, _run("RUNNING"))]
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(200, fini)]
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["done"] is True and out["provider_ok"] is None and out["next_step"]


def test_a_blocked_run_quotes_its_reason(fake, cle, metrage):
    fake.routes[("POST", "/v1/run")] = [Resp(200, _run("BLOCKED", reason="budget-cap-banc"))]
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["done"] is True and out["provider_ok"] is False and out["cost_usd"] is None
    assert "budget-cap-banc" in out["next_step"]
    assert metrage["trace"] == [{"quantity": 1}]


def test_each_terminal_status_has_its_own_next_step(fake, cle):
    suites = []
    for status in ("FAILED", "TIMED_OUT", "STOPPED"):
        fake.routes[("POST", "/v1/run")] = [Resp(408 if status == "TIMED_OUT" else 200,
                                                 _run(status))]
        out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
        assert out["done"] is True and out["provider_ok"] is False and out["next_step"]
        suites.append(out["next_step"])
    assert len(set(suites)) == 3


def test_an_accepted_run_is_waited_for_until_it_finishes(fake, cle, horloge):
    fake.routes[("POST", "/v1/run")] = [Resp(202, _run("READY"))]
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(200, _run("RUNNING")),
                                               Resp(200, _run(cost={"value": 0.25}))]
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["done"] is True and out["cost_usd"] == 0.25 and out["next_step"] is None
    assert len(fake.envois("GET")) == 2


def test_wait_seconds_zero_returns_the_accepted_run_at_once(fake, cle):
    fake.routes[("POST", "/v1/run")] = [Resp(202, _run("READY"))]
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT, wait_seconds=0)
    assert out["done"] is False and out["provider_ok"] is None
    assert fake.envois("GET") == [] and RID in out["next_step"]
    assert f'monid_runs(op="stop", run_id="{RID}")' in out["next_step"]


def test_a_run_still_going_at_the_deadline_is_returned_without_oversleeping(fake, cle, horloge):
    fake.latence = {"POST": 1.0, "GET": 0.5}
    fake.routes[("POST", "/v1/run")] = [Resp(202, _run("READY"))]
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(200, _run("RUNNING"))]
    debut = horloge.t
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT, wait_seconds=20)
    assert out["done"] is False and out["provider_ok"] is None
    assert f'monid_runs(op="get", run_id="{RID}"' in out["next_step"]
    assert sum(horloge.sommeils) <= 20
    assert horloge.t - debut <= 1.0 + 20 + 0.5


def _lancement_lent(fake, latence_post):
    fake.latence = {"POST": latence_post, "GET": 0.5}
    fake.routes[("POST", "/v1/run")] = [Resp(202, _run("READY"))]
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(200, _run("RUNNING"))]


def test_the_wait_shares_one_deadline_with_a_slow_launch(fake, cle, horloge):
    _lancement_lent(fake, 40.0)
    debut = horloge.t
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT, wait_seconds=20)
    assert out["done"] is False
    assert sum(horloge.sommeils) <= 5
    assert horloge.t - debut <= 45 + 0.5


def test_the_tool_wide_deadline_guard_bites(fake, cle, horloge, monkeypatch):
    """Sans l'échéance commune, la même attente repart pour ses 20 s entières."""
    from oto_mcp.tools import monid
    monkeypatch.setattr(monid, "_RUN_BUDGET_S", 10_000)
    _lancement_lent(fake, 40.0)
    _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT, wait_seconds=20)
    assert sum(horloge.sommeils) > 5


def test_a_launch_past_the_deadline_does_not_poll(fake, cle, horloge):
    _lancement_lent(fake, 46.0)
    out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT, wait_seconds=20)
    assert out["done"] is False and fake.envois("GET") == []


def _resolution_lente(monkeypatch, horloge, secondes):
    def _resolve(provider, account=None):
        horloge.t += secondes
        return CLE, False
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", _resolve)


@pytest.mark.parametrize("secondes", [0.0, 4.0, 25.0])
def test_the_launch_read_gets_what_the_key_resolution_left(fake, horloge, monkeypatch,
                                                           secondes):
    """La lecture du POST se taille sur le budget RESTANT : 45 − 10 de connexion − le
    temps de résolution de la clé, au plus 35 — pas 35 quoi qu'il en ait coûté."""
    _resolution_lente(monkeypatch, horloge, secondes)
    fake.routes[("POST", "/v1/run")] = [Resp(200, _run())]
    _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    (envoi,) = fake.envois("POST", "/v1/run")
    assert envoi["timeout"] == (10, pytest.approx(35 - secondes))


def test_too_little_budget_left_sends_nothing(fake, horloge, monkeypatch, metrage):
    _resolution_lente(monkeypatch, horloge, 26.0)
    fake.routes[("POST", "/v1/run")] = [Resp(200, _run())]
    with pytest.raises(McpError) as e:
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert e.value.error.code == INVALID_PARAMS
    assert fake.log == [] and metrage["trace"] == [] and metrage["usage"] == []


@pytest.mark.parametrize("echec", [
    Resp(500, {"code": 500, "message": "boom"}),
    requests.exceptions.ConnectionError("reset"),
    Resp(200, raw="<html>gateway</html>"),
])
def test_a_failed_poll_returns_the_accepted_run_and_the_way_back(fake, cle, metrage, caplog,
                                                                 echec):
    fake.routes[("POST", "/v1/run")] = [Resp(202, _run("READY"))]
    fake.routes[("GET", f"/v1/runs/{RID}")] = [echec]
    with caplog.at_level(logging.WARNING, logger="oto_mcp.tools.monid"):
        out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["done"] is False and out["run"]["status"] == "READY"
    assert f'monid_runs(op="get", run_id="{RID}")' in out["next_step"]
    assert any(RID in r.getMessage() for r in caplog.records)
    assert metrage["trace"] == [{"quantity": 1}]


@pytest.mark.parametrize("releve", [
    [Resp(200, [1, 2]), Resp(200)],                   # une liste, puis un corps vide
    [Resp(200, _run(runId="01JAUTRE0000000000000000000"))],  # le run d'un autre
])
def test_a_poll_that_returns_no_run_keeps_the_accepted_one(fake, cle, caplog, releve):
    fake.routes[("POST", "/v1/run")] = [Resp(202, _run("READY"))]
    fake.routes[("GET", f"/v1/runs/{RID}")] = releve
    with caplog.at_level(logging.WARNING, logger="oto_mcp.tools.monid"):
        out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["run"]["runId"] == RID and out["done"] is False
    assert f'run_id="{RID}"' in out["next_step"] and "None" not in out["next_step"]
    assert any(RID in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("corps", [Resp(200), Resp(200, [1, 2]), Resp(200, {"status": "COMPLETED"})])
def test_get_of_a_body_that_is_no_run_says_it_is_unreadable(fake, cle, corps):
    fake.routes[("GET", f"/v1/runs/{RID}")] = [corps]
    out = _outil("monid_runs")(op="get", run_id=RID)
    assert out["done"] is False and out["provider_ok"] is None
    assert f'run_id="{RID}"' in out["next_step"] and "None" not in out["next_step"]
    assert "pas encore fini" not in out["next_step"]


# --- l'issue inconnue ---------------------------------------------------------------

@pytest.mark.parametrize("reponse", [
    Resp(500, {"code": 500, "message": "internal"}),
    Resp(502, raw="<html>bad gateway</html>"),
    requests.exceptions.ReadTimeout("read timed out"),
    requests.exceptions.ConnectionError("connection reset"),
])
def test_an_unknown_outcome_is_refused_never_retried_never_metered(fake, cle, metrage,
                                                                   reponse):
    fake.routes[("POST", "/v1/run")] = [reponse]
    with pytest.raises(McpError) as e:
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    # Ni « argument invalide » (qui pousse à corriger puis rappeler : le double paiement),
    # ni réessayable.
    assert e.value.error.code == INTERNAL_ERROR
    info = classify(e.value)
    assert info.code != "invalid_input" and info.retryable is False
    assert 'monid_runs(op="list")' in e.value.error.message
    assert len(fake.envois("POST", "/v1/run")) == 1
    assert metrage["trace"] == [] and metrage["usage"] == []
    # Décision assumée : une issue inconnue n'est pas « attendue », elle remonte à Sentry.
    assert _is_expected_error(e.value) is False


def test_under_the_platform_key_the_unknown_outcome_does_not_point_to_the_closed_list(fake,
                                                                                      cle):
    cle["platform"] = True
    fake.routes[("POST", "/v1/run")] = [Resp(500, {"code": 500, "message": "internal"})]
    with pytest.raises(McpError) as e:
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert e.value.error.code == INTERNAL_ERROR
    assert "monid_runs(" not in e.value.error.message


def test_under_the_platform_key_a_404_does_not_point_to_the_closed_list(fake, cle):
    cle["platform"] = True
    fake.routes[("POST", "/v1/inspect")] = [Resp(404, {"code": 404, "message": "nope"})]
    with pytest.raises(McpError) as e:
        _outil("monid_endpoint")(op="inspect", provider=PROVIDER, endpoint=ENDPOINT)
    assert e.value.error.code == INVALID_PARAMS
    assert "monid_runs(" not in e.value.error.message


@pytest.mark.parametrize("appel, reponse", [
    ("get", Resp(200, raw="<html>x</html>")),
    ("get", Resp(302, raw="", headers={"Location": "https://x.test"})),
    ("wallet", Resp(200, raw="<html>x</html>")),
    ("launch", Resp(302, {"message": "moved"})),
])
def test_a_protocol_error_that_ran_nothing_is_a_named_refusal(fake, cle, metrage, appel,
                                                               reponse):
    """Sans `may_have_run`, une réponse inexploitable n'est ni une issue inconnue (la
    marque de celle-ci : la liste des runs) ni une exception brute."""
    route = {"get": ("GET", f"/v1/runs/{RID}"), "wallet": ("GET", "/v1/wallet/balance"),
             "launch": ("POST", "/v1/run")}[appel]
    fake.routes[route] = [reponse]
    outil = {"get": lambda: _outil("monid_runs")(op="get", run_id=RID),
             "wallet": lambda: _outil("monid_wallet")(),
             "launch": lambda: _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)}[appel]
    with pytest.raises(McpError) as e:
        outil()
    assert e.value.error.code == INVALID_PARAMS
    assert classify(e.value).code == "invalid_input"
    assert 'monid_runs(op="list")' not in e.value.error.message
    assert len(fake.envois(*route)) == 1
    assert metrage["trace"] == []


def test_the_unknown_outcome_carries_the_request_id(fake, cle):
    fake.routes[("POST", "/v1/run")] = [Resp(500, {"code": 500, "message": "internal"},
                                             headers={"x-request-id": "req-5xx"})]
    with pytest.raises(McpError) as e:
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert "req-5xx" in e.value.error.message


def test_the_unknown_outcome_names_the_transport_incident_not_the_wrapper(fake, cle):
    fake.routes[("POST", "/v1/run")] = [requests.exceptions.ReadTimeout("read timed out")]
    with pytest.raises(McpError) as e:
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert "ReadTimeout" in e.value.error.message
    assert "MonidProtocolError" not in e.value.error.message


def test_the_same_5xx_off_a_launch_stays_retryable():
    """La garde bite : sans `may_have_run`, un 500 repart tel quel, réessayable."""
    from oto_mcp.tools import monid_socle
    lecture = MonidHTTPError(500, {"code": 500, "message": "internal"})
    lancement = MonidHTTPError(500, {"code": 500, "message": "internal"})
    lancement.may_have_run = True
    assert monid_socle._traduire(lecture) is lecture and classify(lecture).retryable is True
    assert isinstance(monid_socle._traduire(lancement), McpError)


def test_a_connect_timeout_propagates_raw_and_is_a_retryable_timeout(fake, cle, metrage):
    fake.routes[("POST", "/v1/run")] = [requests.exceptions.ConnectTimeout(
        "Connection to api.monid.ai timed out. (connect timeout=10)")]
    with pytest.raises(requests.exceptions.ConnectTimeout) as e:
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    info = classify(e.value)
    assert info.code == "upstream_timeout" and info.retryable is True
    assert metrage["trace"] == []


# --- métrage -----------------------------------------------------------------------

def test_the_platform_key_debits_the_internal_quota(fake, cle, metrage):
    cle["platform"] = True
    fake.routes[("POST", "/v1/run")] = [Resp(200, _run())]
    _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert metrage["trace"] == [{"quantity": 1}] and metrage["usage"] == [("monid", 1)]


@pytest.mark.parametrize("panne, lancement", [
    (psycopg.OperationalError("base injoignable"), [Resp(200, _run())]),
    (psycopg_pool.PoolTimeout("pool épuisé"), [Resp(202, _run("READY"))]),
])
def test_a_failed_quota_debit_never_hides_a_launched_run(fake, cle, metrage, monkeypatch,
                                                         caplog, panne, lancement):
    """Le run est lancé et facturé : une panne du compteur ne doit pas le rendre en
    « erreur interne » sans son identifiant — l'agent relancerait, et paierait deux fois."""
    cle["platform"] = True

    def _debit_en_panne(provider, calls=1):
        raise panne

    monkeypatch.setattr("oto_mcp.access.record_platform_usage", _debit_en_panne)
    fake.routes[("POST", "/v1/run")] = lancement
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(200, _run())]
    with caplog.at_level(logging.WARNING, logger="oto_mcp.tools.monid"):
        out = _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert out["run"]["runId"] == RID and out["done"] is True
    assert len(fake.envois("POST", "/v1/run")) == 1
    assert metrage["trace"] == [{"quantity": 1}]
    assert any(r.levelno >= logging.WARNING and RID in r.getMessage() and r.exc_info
               for r in caplog.records if r.name == "oto_mcp.tools.monid")


def test_a_refused_launch_meters_nothing(fake, cle, metrage):
    cle["platform"] = True
    fake.routes[("POST", "/v1/run")] = [Resp(402, {"code": 402, "message": "wallet"})]
    with pytest.raises(McpError):
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert metrage["trace"] == [] and metrage["usage"] == []


# --- traduction des refus, par code --------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 422])
def test_a_client_refusal_becomes_a_named_invalid_params(fake, cle, status):
    fake.routes[("GET", "/v1/wallet/balance")] = [Resp(status, {"code": status,
                                                                "message": "refus"})]
    with pytest.raises(McpError) as e:
        _outil("monid_wallet")()
    assert e.value.error.code == INVALID_PARAMS
    assert "req-banc" in e.value.error.message
    assert classify(e.value).code == "invalid_input"


@pytest.mark.parametrize("status, code", [(429, "rate_limited"), (500, "upstream_5xx"),
                                          (502, "upstream_5xx"), (503, "upstream_5xx")])
def test_429_and_5xx_are_left_retryable_to_the_taxonomy(fake, cle, status, code):
    fake.routes[("GET", "/v1/wallet/balance")] = [Resp(status, {"code": status, "message": "x"})]
    with pytest.raises(MonidHTTPError) as e:
        _outil("monid_wallet")()
    assert e.value.status_code == status
    info = classify(e.value)
    assert info.code == code and info.retryable is True


def test_a_monid_402_on_launch_is_a_wallet_refusal_that_names_whose_wallet(fake, cle):
    fake.routes[("POST", "/v1/run")] = [Resp(402, {"code": 402, "message": "insufficient"})]
    messages = []
    for platform in (False, True):
        cle["platform"] = platform
        with pytest.raises(McpError) as e:
            _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
        assert e.value.error.code == INVALID_PARAMS
        messages.append(e.value.error.message)
    assert messages[0] != messages[1]


def test_a_401_on_launch_is_a_named_refusal(fake, cle):
    fake.routes[("POST", "/v1/run")] = [Resp(401, {"code": 401, "message": "Invalid API key format"})]
    with pytest.raises(McpError) as e:
        _outil("monid_run")(provider=PROVIDER, endpoint=ENDPOINT)
    assert e.value.error.code == INVALID_PARAMS
    assert len(fake.envois("POST")) == 1


def test_a_wallet_that_failed_is_not_left_retryable(fake, cle):
    fake.routes[("GET", "/v1/wallet/balance")] = [
        Resp(503, {"code": 503, "message": "wallet", "walletStatus": "FAILED"})]
    with pytest.raises(McpError) as e:
        _outil("monid_wallet")()
    assert e.value.error.code == INVALID_PARAMS and "FAILED" in e.value.error.message
    assert len(fake.envois("GET")) == 1


def test_a_wallet_provisioning_too_long_stays_retryable(fake, cle, horloge):
    fake.routes[("GET", "/v1/wallet/balance")] = [
        Resp(503, {"code": 503, "message": "wallet", "walletStatus": "PROVISIONING"},
             headers={"Retry-After": "30"})]
    with pytest.raises(MonidHTTPError) as e:
        _outil("monid_wallet")()
    assert e.value.retry_after == 30 and horloge.sommeils == []


def test_the_wallet_is_returned_as_monid_returns_it(fake, cle):
    solde = {"balance": {"value": -1.5, "currency": "USD"}, "held": {"value": 2, "currency": "USD"}}
    fake.routes[("GET", "/v1/wallet/balance")] = [Resp(200, solde)]
    assert _outil("monid_wallet")() == solde


SOLDE = {"balance": {"value": 12.5, "currency": "USD"}, "held": {"value": 0, "currency": "USD"}}


def test_the_wallet_is_refused_under_the_platform_key(fake, cle):
    """Sous la clé plateforme, le portefeuille est celui, PARTAGÉ, de la plateforme : son
    solde n'est pas servi aux orgs qui ont un grant. Refusé en le nommant, avant tout envoi."""
    cle["platform"] = True
    fake.routes[("GET", "/v1/wallet/balance")] = [Resp(200, SOLDE)]
    with pytest.raises(McpError) as e:
        _outil("monid_wallet")()
    assert e.value.error.code == INVALID_PARAMS
    assert classify(e.value).code == "invalid_input"
    assert "clé de la plateforme" in e.value.error.message
    assert "402" in e.value.error.message and "propre clé Monid" in e.value.error.message
    assert fake.log == []


def test_the_wallet_is_still_served_under_a_byo_key(fake, cle):
    cle["platform"] = False
    fake.routes[("GET", "/v1/wallet/balance")] = [Resp(200, SOLDE)]
    assert _outil("monid_wallet")() == SOLDE
    assert len(fake.envois("GET", "/v1/wallet/balance")) == 1


def test_the_platform_wallet_guard_bites(fake, cle, monkeypatch):
    """Sans `_garde_solde`, le solde du portefeuille partagé partirait à l'org."""
    from oto_mcp.tools import monid
    cle["platform"] = True
    fake.routes[("GET", "/v1/wallet/balance")] = [Resp(200, SOLDE)]
    monkeypatch.setattr(monid, "_garde_solde", lambda is_platform: None)
    assert _outil("monid_wallet")() == SOLDE
    assert len(fake.envois("GET", "/v1/wallet/balance")) == 1


# --- les runs ------------------------------------------------------------------------

def test_the_run_list_names_what_it_drops_and_nulls_the_last_cursor(fake, cle):
    item = {"runId": RID, "caller": "USER#banc", "status": "COMPLETED", "cost": {"value": 0.1}}
    fake.routes[("GET", "/v1/runs")] = [Resp(200, {"items": [item], "cursor": "c2"}),
                                        Resp(200, {"items": [item]})]
    page = _outil("monid_runs")(limit=5, status="running")
    assert fake.log[-1]["params"] == {"limit": 5, "status": "RUNNING"}
    assert page["cursor"] == "c2" and "caller" not in page["items"][0]
    assert page["projection"]["omitted"] == ["caller"]
    derniere = _outil("monid_runs")(cursor="c2", full=True)
    assert fake.log[-1]["params"] == {"limit": 20, "cursor": "c2"}
    assert derniere["cursor"] is None and derniere["items"][0]["caller"] == "USER#banc"


def test_the_run_list_is_refused_under_the_platform_key(fake, cle):
    """Le workspace de la clé plateforme est PARTAGÉ : sa liste montrerait les runs des
    autres orgs. Refusée avant tout envoi."""
    cle["platform"] = True
    fake.routes[("GET", "/v1/runs")] = [Resp(200, {"items": [{"runId": RID}]})]
    with pytest.raises(McpError) as e:
        _outil("monid_runs")()
    assert e.value.error.code == INVALID_PARAMS
    assert fake.log == []


def test_the_platform_list_guard_bites(fake, cle, monkeypatch):
    from oto_mcp.tools import monid
    cle["platform"] = True
    fake.routes[("GET", "/v1/runs")] = [Resp(200, {"items": [{"runId": RID}]})]
    monkeypatch.setattr(monid, "_garde_liste", lambda is_platform: None)
    assert _outil("monid_runs")()["items"] == [{"runId": RID}]
    assert len(fake.envois("GET", "/v1/runs")) == 1


def test_get_and_stop_by_id_still_work_under_the_platform_key(fake, cle):
    cle["platform"] = True
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(200, _run())]
    fake.routes[("POST", f"/v1/runs/{RID}/stop")] = [
        Resp(202, {"runId": RID, "status": "STOPPING", "message": "stop requested"})]
    assert _outil("monid_runs")(op="get", run_id=RID)["run"]["runId"] == RID
    assert _outil("monid_runs")(op="stop", run_id=RID)["status"] == "STOPPING"
    assert [(e["method"], e["path"]) for e in fake.log] == [
        ("GET", f"/v1/runs/{RID}"), ("POST", f"/v1/runs/{RID}/stop")]


def test_an_unknown_status_filter_is_refused_before_sending(fake, cle):
    with pytest.raises(McpError):
        _outil("monid_runs")(status="DONE")
    assert fake.log == []


def test_get_reads_one_run_escaped_in_the_path(fake, cle):
    fake.routes[("GET", "/v1/runs/a%2Fb")] = [Resp(200, _run(runId="a/b"))]
    out = _outil("monid_runs")(op="get", run_id="a/b")
    assert out["done"] is True and out["run"]["runId"] == "a/b"
    assert [e["path"] for e in fake.log] == ["/v1/runs/a%2Fb"]


def test_get_with_wait_polls_until_the_run_finishes(fake, cle, horloge):
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(200, _run("RUNNING")),
                                               Resp(200, _run("RUNNING")),
                                               Resp(200, _run())]
    out = _outil("monid_runs")(op="get", run_id=RID, wait_seconds=10)
    assert out["done"] is True and len(fake.log) == 3
    assert sum(horloge.sommeils) <= 10


def test_an_unknown_run_is_a_named_refusal(fake, cle):
    fake.routes[("GET", f"/v1/runs/{RID}")] = [Resp(404, {"code": 404, "message": "Run not found"})]
    with pytest.raises(McpError) as e:
        _outil("monid_runs")(op="get", run_id=RID)
    assert e.value.error.code == INVALID_PARAMS


def test_stop_is_sent_once_and_says_it_is_asynchronous(fake, cle):
    fake.routes[("POST", f"/v1/runs/{RID}/stop")] = [
        Resp(202, {"runId": RID, "status": "STOPPING", "message": "stop requested"})]
    out = _outil("monid_runs")(op="stop", run_id=RID)
    assert (out["run_id"], out["status"], out["message"]) == (RID, "STOPPING", "stop requested")
    assert f'run_id="{RID}"' in out["next_step"]
    assert len(fake.log) == 1


def test_stopping_a_finished_run_is_a_named_refusal_not_retried(fake, cle):
    fake.routes[("POST", f"/v1/runs/{RID}/stop")] = [Resp(409, {"code": 409, "message": "terminal"})]
    with pytest.raises(McpError) as e:
        _outil("monid_runs")(op="stop", run_id=RID)
    assert e.value.error.code == INVALID_PARAMS and len(fake.log) == 1


# --- la sonde ------------------------------------------------------------------------

def test_the_probe_reads_whoami_only_and_names_the_identity(fake):
    from oto_mcp.tools import monid
    fake.routes[("GET", "/v1/auth/whoami")] = [Resp(200, {
        "user": {"userId": "user_banc", "username": ""},
        "workspace": {"workspaceId": "org_banc", "name": "", "slug": "banc"}})]
    out = monid._verify({"key": CLE})
    assert out == {"identity": {"workspace_id": "org_banc", "workspace": "banc",
                                "user_id": "user_banc"}}
    assert [(e["method"], e["path"]) for e in fake.log] == [("GET", "/v1/auth/whoami")]
    assert fake.log[0]["auth"] == f"Bearer {CLE}"


def test_the_probe_measures_nothing_it_was_not_told(fake):
    from oto_mcp.tools import monid
    fake.routes[("GET", "/v1/auth/whoami")] = [Resp(200, {"user": {}})]
    assert monid._verify({"key": CLE}) is None


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_classified_unauthorized(fake, status):
    from oto_mcp.tools import monid
    fake.routes[("GET", "/v1/auth/whoami")] = [Resp(status, {"code": status, "message": "no"})]
    with pytest.raises(connector_verify.NonAutorise) as e:
        monid._verify({"key": CLE})
    assert connector_verify.classer(e.value) == connector_verify.UNAUTHORIZED
