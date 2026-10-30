"""Les tools `search_console*` (Google Search Console, compte de service).

Le VRAI client oto-core sur une doublure de transport, comme les tools `ga4_*`. Ce qui
est verrouillé ici : la paire, le credential à un champ, la table de performance, la
fenêtre par défaut, l'inspection seule et par lots, les refus nommés, et la sonde
(identité + propriétés lisibles, zéro = refus).

Aucun identifiant réel : site et email sont factices, la clé RSA est générée à la volée.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from urllib.parse import urlparse

import pytest
import requests
from requests.adapters import BaseAdapter

from oto_mcp.mcp_errors import McpError

pytestmark = pytest.mark.exige_pin_oto_core

EMAIL = "lecteur@projet-factice.iam.gserviceaccount.com"
SITE = "sc-domain:exemple.test"
SITE_PATH = "/webmasters/v3/sites/sc-domain%3Aexemple.test"
INSPECT = "/v1/urlInspection/index:inspect"


@pytest.fixture(scope="module")
def key_json() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    return json.dumps({"type": "service_account", "private_key_id": "kid-factice",
                       "private_key": pem, "client_email": EMAIL,
                       "token_uri": "https://oauth2.googleapis.com/token"})


class _Transport(BaseAdapter):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.routes = {("POST", "/token"): (200, {"access_token": "jeton", "expires_in": 3599})}

    def on(self, method, path, status, body):
        self.routes[(method, path)] = (status, body)

    def send(self, request, **kwargs):
        self.sent.append(request)
        u = urlparse(request.url)
        path = "/token" if u.netloc == "oauth2.googleapis.com" else u.path
        if (request.method, path) not in self.routes:
            raise AssertionError(f"requête inattendue : {request.method} {request.url}")
        status, body = self.routes[(request.method, path)]
        if callable(body):
            status, body = body(request)
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode() if body is not None else b""
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch, key_json):
    from oto.tools.google_analytics import auth as ga_auth
    from oto.tools.search_console import client as sc_client
    from oto_mcp import access

    ga_auth._TOKEN_CACHE.clear()
    t = _Transport()
    vraie_session = requests.Session

    def session():
        s = vraie_session()
        s.mount("https://", t)
        return s

    monkeypatch.setattr(sc_client.requests, "Session", session)
    monkeypatch.setattr(access, "resolve_credential_fields",
                        lambda name: {"service_account_json": key_json})
    return t


@pytest.fixture(scope="module")
def tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import search_console as svc

    m = FastMCP("t")
    svc.register(m)
    return {t.name: t.fn for t in asyncio.run(m.list_tools(run_middleware=False))}


def _refus(status, reason):
    return {"error": {"code": status, "message": "refus factice",
                      "errors": [{"reason": reason}]}}


def test_la_paire_et_le_credential():
    from oto_mcp import providers
    from oto_mcp.tools import search_console as svc

    c = providers.connector_for_namespace("search_console")
    assert c.name == "search_console" and c.secret_kind == "fields"
    assert [f.name for f in c.credential_fields] == ["service_account_json"]
    assert c.credential_fields[0].whitespace_significant is True
    assert svc._default_range(date(2026, 10, 30)) == ("2026-10-01", "2026-10-28")


def test_tools_servis(tools):
    assert set(tools) == {"search_console", "search_console_act"}


def test_sites_ecarte_les_proprietes_non_verifiees(tools, transport):
    transport.on("GET", "/webmasters/v3/sites", 200, {"siteEntry": [
        {"siteUrl": SITE, "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://autre.test/", "permissionLevel": "siteUnverifiedUser"}]})
    assert [s["siteUrl"] for s in tools["search_console"]()["sites"]] == [SITE]


def test_performance_en_table_avec_la_fenetre_par_defaut(tools, transport):
    transport.on("POST", f"{SITE_PATH}/searchAnalytics/query", 200, {"rows": [
        {"keys": ["q1"], "clicks": 1, "impressions": 9, "ctr": 0.11, "position": 8.5}]})
    out = tools["search_console"](op="performance", site=SITE, dimensions=["query"], row_limit=1)
    assert out["columns"][:2] == ["query", "clicks"] and out["rows"][0][0] == "q1"
    assert out["next_row"] == 1
    corps = json.loads(transport.sent[-1].body)
    assert corps["startDate"] < corps["endDate"]


def test_inspect_exige_url_ou_urls(tools, transport):
    with pytest.raises(McpError, match="exactly one"):
        tools["search_console"](op="inspect", site=SITE)
    with pytest.raises(McpError, match="exactly one"):
        tools["search_console"](op="inspect", site=SITE, url="https://exemple.test/a",
                                urls=["https://exemple.test/b"])
    assert transport.sent == []


def test_inspect_seul_dit_si_la_page_est_indexee(tools, transport):
    transport.on("POST", INSPECT, 200, {"inspectionResult": {"indexStatusResult": {
        "verdict": "PASS", "coverageState": "Submitted and indexed"}}})
    out = tools["search_console"](op="inspect", site=SITE, url="https://exemple.test/a")
    assert out == {"url": "https://exemple.test/a", "verdict": "PASS",
                   "coverage": "Submitted and indexed"}


def test_un_lot_s_arrete_sur_un_quota_et_garde_ce_qu_il_a_lu(tools, transport):
    reponses = iter([(200, {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}),
                     (429, _refus(429, "rateLimitExceeded"))])
    transport.on("POST", INSPECT, 200, lambda req: next(reponses))
    urls = [f"https://exemple.test/{c}" for c in "abc"]
    out = tools["search_console"](op="inspect", site=SITE, urls=urls)
    assert out["total"] == 3 and len(out["inspected"]) == 1
    assert [f["url"] for f in out["failed"]] == urls[1:]
    assert "quota" in out["failed"][0]["error"]
    assert out["failed"][1]["error"].startswith("not read")


def test_un_lot_trop_long_est_refuse_sans_appel(tools, transport):
    with pytest.raises(McpError, match="at most 20"):
        tools["search_console"](op="inspect", site=SITE,
                                urls=[f"https://exemple.test/{i}" for i in range(21)])
    assert transport.sent == []


def test_add_sitemap(tools, transport):
    transport.on("PUT", f"{SITE_PATH}/sitemaps/https%3A%2F%2Fexemple.test%2Fsitemap.xml", 204, None)
    out = tools["search_console_act"](op="add_sitemap", site=SITE,
                                      sitemap="https://exemple.test/sitemap.xml")
    assert out["submitted"] is True


@pytest.mark.parametrize("status,raison,attendu", [
    (403, "forbidden", EMAIL),
    (403, "accessNotConfigured", "not enabled"),
    (400, "badRequest", "belong to the property"),
])
def test_les_refus_sont_nommes(tools, transport, status, raison, attendu):
    transport.on("GET", f"{SITE_PATH}/sitemaps", status, _refus(status, raison))
    with pytest.raises(McpError, match=attendu):
        tools["search_console"](op="sitemaps", site=SITE)


def test_sonde_identite_et_zero_propriete(transport, key_json):
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import search_console as svc

    transport.on("GET", "/webmasters/v3/sites", 200, {"siteEntry": [
        {"siteUrl": SITE, "permissionLevel": "siteRestrictedUser"}]})
    ok = svc._verify({"service_account_json": key_json})
    assert ok == {"identity": {"service_account": EMAIL, "sites": 1}}

    transport.on("GET", "/webmasters/v3/sites", 200, {"siteEntry": []})
    with pytest.raises(connector_verify.NonAutorise, match=EMAIL):
        svc._verify({"service_account_json": key_json})
