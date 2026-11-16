"""Tool `search_console` (neuvième service Google, 2026-11-15).

Ce que ce fichier verrouille :
1. LECTURE SEULE — l'outil est annoté LECTURE, ne sert aucune op d'écriture, et le
   consentement demandé est `webmasters.readonly` du service `search_console` ;
2. le JETON — dans l'en-tête `Authorization`, jamais dans l'URL ; aucune redirection
   suivie ;
3. les ROUTES — la propriété voyage en UN segment de chemin (`sc-domain%3Aexemple.test`),
   le corps de `query_search_analytics` reprend les noms de Google ;
4. les refus AVANT tout appel (propriété mal formée, dates, dimension inconnue) et les
   refus Google NOMMÉS (API non activée, propriété non lisible, quota).

`requests.request` remplacé au seam du module : ni serveur, ni coffre, ni Google.
"""
import asyncio
import json

import pytest

from oto_mcp.mcp_errors import McpError

API = "https://searchconsole.googleapis.com"
SITE = "sc-domain:exemple.test"


def _tool():
    from fastmcp import FastMCP
    from oto_mcp.tools import search_console as T

    m = FastMCP("t")
    T.register(m)
    tool = asyncio.run(m.get_tool("search_console"))
    return tool


def _call(**kw):
    return asyncio.run(_tool().fn(**kw))


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.content = json.dumps(body).encode() if body is not None else b""
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def google(monkeypatch):
    from oto_mcp.tools import search_console as T

    class _Google:
        def __init__(self):
            self.calls = []
            self.replies = [(200, {})]

        def __call__(self, method, url, **kw):
            self.calls.append({"method": method, "url": url, **kw})
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            return _Resp(*reply)

    g = _Google()
    monkeypatch.setattr(T.requests, "request", g)
    monkeypatch.setattr(T, "_token_for_user", lambda account=None: "ya29.jeton")
    return g


# --- 1. lecture seule ---------------------------------------------------------------

def test_l_outil_est_une_lecture_sans_op_d_ecriture():
    tool = _tool()
    assert tool.annotations.readOnlyHint is True
    ops = tool.parameters["properties"]["op"]
    enum = ops.get("enum") or [e for a in ops.get("anyOf", []) for e in a.get("enum", [])]
    assert set(enum) == {"list_sites", "query_search_analytics", "inspect_url",
                         "list_sitemaps", "get_sitemap"}
    assert tool.description and "read-only" in tool.description


def test_le_jeton_est_celui_du_service_search_console(monkeypatch):
    from oto_mcp.tools import search_console as T

    vus = []

    class _Creds:
        token = "ya29.x"

    def credentials_for(sub, account=None, service=None):
        vus.append((sub, account, service))
        return _Creds()

    monkeypatch.setattr(T.access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(T.google_oauth, "credentials_for", credentials_for)
    assert T._token_for_user("a@x.test") == "ya29.x"
    assert vus == [("u1", "a@x.test", "search_console")]


def test_un_compte_qui_na_pas_autorise_search_console_est_un_refus_nomme(monkeypatch):
    from oto_mcp.tools import search_console as T

    def credentials_for(sub, account=None, service=None):
        raise RuntimeError("has not yet authorized Google Search Console")

    monkeypatch.setattr(T.access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(T.google_oauth, "credentials_for", credentials_for)
    with pytest.raises(McpError, match="Search Console"):
        T._token_for_user()


# --- 2 & 3. jeton et routes ------------------------------------------------------------

def test_list_sites_par_defaut_jeton_en_entete(google):
    google.replies = [(200, {"siteEntry": [{"siteUrl": SITE,
                                            "permissionLevel": "siteOwner"}]})]
    out = _call()
    assert out["siteEntry"][0]["siteUrl"] == SITE
    (c,) = google.calls
    assert (c["method"], c["url"]) == ("GET", f"{API}/webmasters/v3/sites")
    assert c["headers"] == {"Authorization": "Bearer ya29.jeton"}
    assert "ya29" not in c["url"] and c["allow_redirects"] is False


def test_query_search_analytics_un_segment_et_les_noms_de_google(google):
    google.replies = [(200, {"rows": [{"keys": ["oto"], "clicks": 3}]})]
    filtres = [{"filters": [{"dimension": "page", "operator": "contains",
                             "expression": "/blog/"}]}]
    _call(op="query_search_analytics", site_url=SITE, start_date="2026-10-01",
          end_date="2026-10-31", dimensions=["query"], row_limit=100,
          dimension_filter_groups=filtres)
    (c,) = google.calls
    assert c["url"] == f"{API}/webmasters/v3/sites/sc-domain%3Aexemple.test/searchAnalytics/query"
    assert c["json"] == {"startDate": "2026-10-01", "endDate": "2026-10-31", "type": "web",
                         "aggregationType": "auto", "dataState": "final", "rowLimit": 100,
                         "startRow": 0, "dimensions": ["query"],
                         "dimensionFilterGroups": filtres}


def test_une_propriete_prefixe_d_url_est_encodee_entiere(google):
    google.replies = [(200, {"sitemap": [{"path": "https://www.exemple.test/sitemap.xml"}]})]
    out = _call(op="list_sitemaps", site_url="https://www.exemple.test/")
    assert out["items"][0]["path"].endswith("sitemap.xml")
    assert google.calls[0]["url"] == (f"{API}/webmasters/v3/sites/"
                                      "https%3A%2F%2Fwww.exemple.test%2F/sitemaps")


def test_get_sitemap_et_inspect_url(google):
    _call(op="get_sitemap", site_url=SITE, feedpath="https://www.exemple.test/sitemap.xml")
    _call(op="inspect_url", site_url=SITE, inspection_url="https://www.exemple.test/prix",
          language_code="fr-FR")
    get, inspect = google.calls
    assert get["url"].endswith("/sitemaps/https%3A%2F%2Fwww.exemple.test%2Fsitemap.xml")
    assert (inspect["method"], inspect["url"]) == ("POST", f"{API}/v1/urlInspection/index:inspect")
    assert inspect["json"] == {"siteUrl": SITE, "inspectionUrl": "https://www.exemple.test/prix",
                               "languageCode": "fr-FR"}


def test_list_sitemaps_vide_rend_une_liste(google):
    google.replies = [(200, {})]
    assert _call(op="list_sitemaps", site_url=SITE) == {"items": []}


# --- 4. refus ----------------------------------------------------------------------------

@pytest.mark.parametrize("kw, motif", [
    ({"op": "query_search_analytics", "site_url": "exemple.test",
      "start_date": "2026-10-01", "end_date": "2026-10-31"}, "trailing slash"),
    ({"op": "query_search_analytics", "site_url": "https://www.exemple.test",
      "start_date": "2026-10-01", "end_date": "2026-10-31"}, "trailing slash"),
    ({"op": "query_search_analytics", "site_url": SITE, "start_date": "01/10/2026",
      "end_date": "2026-10-31"}, "YYYY-MM-DD"),
    ({"op": "query_search_analytics", "site_url": SITE, "start_date": "2026-10-31",
      "end_date": "2026-10-01"}, "before"),
    ({"op": "query_search_analytics", "site_url": SITE, "start_date": "2026-10-01",
      "end_date": "2026-10-31", "dimensions": ["hour"]}, "hourly_all"),
    ({"op": "query_search_analytics", "site_url": SITE, "start_date": "2026-10-01",
      "end_date": "2026-10-31", "row_limit": 30000}, "25,000"),
    ({"op": "query_search_analytics", "site_url": SITE}, "start_date"),
    ({"op": "inspect_url", "site_url": SITE}, "inspection_url"),
    ({"op": "get_sitemap", "site_url": SITE}, "feedpath"),
    ({"op": "list_sitemaps"}, "site_url"),
])
def test_refus_avant_tout_appel(google, kw, motif):
    with pytest.raises(McpError, match=motif):
        _call(**kw)
    assert google.calls == []


@pytest.mark.parametrize("status, corps, motif", [
    (403, {"error": {"message": "Google Search Console API has not been used",
                     "errors": [{"reason": "accessNotConfigured"}]}}, "not enabled"),
    (403, {"error": {"message": "User does not have sufficient permission for site "
                                "'sc-domain:exemple.test'.",
                     "errors": [{"reason": "forbidden"}]}}, "cannot read this property"),
    (403, {"error": {"message": "Quota exceeded",
                     "errors": [{"reason": "quotaExceeded"}]}}, "quota"),
    (429, {"error": {"message": "Too many requests"}}, "throttling"),
    (401, {"error": {"message": "Invalid Credentials"}}, "reconnect"),
    (404, {"error": {"message": "Not found"}}, "Not found"),
    (400, {"error": {"message": "Invalid dimension"}}, "invalid"),
])
def test_refus_google_nommes(google, status, corps, motif):
    google.replies = [(status, corps)]
    with pytest.raises(McpError, match=motif):
        _call(op="list_sitemaps", site_url=SITE)


def test_chaque_description_dit_quand_prendre_l_autre():
    """Deux connecteurs, une même API : la description servie de chacun dit lequel
    prendre selon l'accès — la connexion Google de la personne ou le compte de service
    de l'org, seul à soumettre un sitemap."""
    from fastmcp import FastMCP
    from oto_mcp import providers
    from oto_mcp.connectors import descriptions
    from oto_mcp.tools import decrits

    assert "`google_search_console`" in _tool().description
    c = providers.REGISTRY["google_search_console"]
    lecture = decrits.monter(FastMCP("t"), c.name, descriptions.charger(c.name), c.tool_note)[0]
    assert lecture.name == "google_search_console"
    assert "`search_console`" in lecture.description and "sitemap" in c.tool_note
