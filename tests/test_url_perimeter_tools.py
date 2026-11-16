"""Le périmètre d'URL (#605), branché outil par outil — et l'identité sans projet.

Chaque outil couvert est exercé DEUX fois avec le même faux amont : sous un périmètre
(le profil personnel est écarté et compté, ou l'URL refusée AVANT tout appel amont),
puis sans (la réponse est celle de l'amont, à l'identique). C'est la preuve demandée :
sans projet ou sans option, aucun changement.

Le périmètre est injecté par `url_perimeter.perimeter_of_call` — sa résolution (jeton
`_project=`, endpoint publié, aucun) est prouvée dans `tests/test_url_perimeter.py` ;
ici on prouve que chaque outil la CONSULTE et en tire le bon effet.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import url_perimeter as up

PER = up.Perimeter(project_id=12, project_name="Campagne",
                   prefixes=(up.parse_prefix("linkedin.com/in/"),))
PROFILE = "https://fr.linkedin.com/in/jane-doe"
COMPANY = "https://www.linkedin.com/company/acme"


@pytest.fixture(params=["perimetre", "sans"], ids=["perimetre", "sans-projet"])
def per(request, monkeypatch):
    """Le même test tourne sous périmètre ET sans : `None` = pas de projet / pas d'option."""
    value = PER if request.param == "perimetre" else None
    monkeypatch.setattr(up, "perimeter_of_call", lambda: value)
    return value


def _tool(module, name):
    from fastmcp import FastMCP
    m = FastMCP("t")
    module.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _assert_effect(out, raw, per, *, path, key="link"):
    """Sous périmètre : le profil est parti, la page entreprise reste, le compte est dit.
    Sans : la réponse EST l'amont (même objet, pas une copie égale)."""
    if per is None:
        assert out is raw
        assert "excluded_by_perimeter" not in out
        return
    items = out
    for p in path:
        items = items[p]
    assert [it[key] if isinstance(it, dict) else it for it in items] == [COMPANY]
    assert out["excluded_by_perimeter"]["count"] == 1
    assert out["excluded_by_perimeter"]["project"] == "Campagne"
    assert out["excluded_by_perimeter"]["prefixes"] == {"linkedin.com/in/": 1}


def _results(key="link"):
    return [{"title": "Jane", key: PROFILE}, {"title": "ACME", key: COMPANY}]


# ── serper ────────────────────────────────────────────────────────────────────

@pytest.fixture
def serper_client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.serper.SerperClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda p, account=None: ("k", False))
    # `serper_scrape` a désormais DEUX amonts : le scraper hébergé, et NOTRE
    # propre lecture du HTML — celle qui récupère les adresses qu'un rendu
    # markdown fait disparaître (#681). Un test qui bouchonne l'un doit
    # bouchonner l'autre, sinon il part vraiment sur le réseau.
    monkeypatch.setattr("oto_mcp.tools.mail_obfuscation.fetch",
                        lambda url, deadline_s=None: {
                            "ok": True, "status": 200, "verdict": "lu",
                            "html": "<p>rien de caché</p>", "final_url": url})
    return inst


def test_serper_search_drops_profiles_and_keeps_the_company_page(serper_client, per):
    from oto_mcp.tools import serper
    raw = {"organic": _results(), "credits": 1}
    serper_client.search.return_value = raw
    out = _tool(serper, "serper_search")(query="jane doe", full=True)
    _assert_effect(out, raw, per, path=("organic",))


def test_serper_search_filters_before_projection(serper_client):
    """`fields=["title"]` retire `link` de chaque résultat : le filtre doit avoir agi
    AVANT, sinon un profil sans son lien passerait à travers."""
    from oto_mcp.tools import serper
    with patch.object(up, "perimeter_of_call", return_value=PER):
        serper_client.search.return_value = {"organic": _results()}
        out = _tool(serper, "serper_search")(query="q", fields=["title"])
    assert out["organic"] == [{"title": "ACME"}]
    assert out["excluded_by_perimeter"]["count"] == 1


def test_serper_search_places_keep_their_website(serper_client):
    """Un lieu n'est pas une page : `website` n'est pas un lien de résultat."""
    from oto_mcp.tools import serper
    with patch.object(up, "perimeter_of_call", return_value=PER):
        serper_client.search_places.return_value = {
            "places": [{"title": "Bar", "website": "https://linkedin.com/in/bar"}]}
        out = _tool(serper, "serper_search")(query="q", kind="places")
    assert len(out["places"]) == 1 and out["excluded_by_perimeter"]["count"] == 0


def test_serper_scrape_refuses_a_profile_before_any_upstream_call(serper_client, per):
    from oto_mcp.tools import serper
    serper_client.scrape_page.return_value = {"markdown": "…"}
    fn = _tool(serper, "serper_scrape")
    if per is None:
        assert fn(url=PROFILE) == {"markdown": "…"}
        return
    with pytest.raises(McpError) as e:
        fn(url=PROFILE)
    assert "linkedin.com/in/" in str(e.value) and "Campagne" in str(e.value)
    serper_client.scrape_page.assert_not_called()
    # la page entreprise, elle, se lit
    assert fn(url=COMPANY) == {"markdown": "…"}


def test_serper_lens_refuses_the_image_url_and_filters_matches(serper_client):
    from oto_mcp.tools import serper
    with patch.object(up, "perimeter_of_call", return_value=PER):
        fn = _tool(serper, "serper_lens")
        with pytest.raises(McpError):
            fn(url="https://linkedin.com/in/jane/photo.jpg")
        serper_client.search_lens.assert_not_called()
        serper_client.search_lens.return_value = {"organic": _results()}
        out = fn(url="https://cdn.example.com/photo.jpg")
    assert [r["link"] for r in out["organic"]] == [COMPANY]


# ── serpapi / searchapi / cloro ───────────────────────────────────────────────

def test_serpapi_search_filters_any_engine_payload(monkeypatch, per):
    from oto_mcp.tools import serpapi
    inst = MagicMock()
    raw = {"organic_results": _results(), "search_metadata": {"id": "x"}}
    inst.search.return_value = raw
    monkeypatch.setattr("oto.tools.serpapi.client.SerpAPIClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda p: ("k", False))
    out = _tool(serpapi, "serpapi_search")(engine="bing", query="jane")
    _assert_effect(out, raw, per, path=("organic_results",))


def test_searchapi_search_filters_the_raw_payload(monkeypatch, per):
    from oto_mcp.tools import searchapi as S
    inst = MagicMock()
    raw = {"organic_results": _results()}
    inst.search.return_value = raw
    monkeypatch.setattr(S, "SearchApiClient", lambda key: inst)
    monkeypatch.setattr(S.access, "resolve_api_key", lambda p: ("k", False))
    out = _tool(S, "searchapi_search")(engine="google", query="jane")
    _assert_effect(out, raw, per, path=("organic_results",))


def test_cloro_google_and_ask_filter_their_sources(monkeypatch, per):
    from oto_mcp.tools import cloro
    import oto.tools.cloro.client as cc
    inst = MagicMock()
    raw = {"organicResults": _results("url")}
    inst.google.return_value = raw
    inst.monitor.return_value = {"answer": "…", "sources": _results("url")}
    monkeypatch.setattr(cc, "CloroClient", MagicMock(return_value=inst))
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda p: ("k", False))
    out = _tool(cloro, "cloro_google")(query="jane")
    _assert_effect(out, raw, per, path=("organicResults",), key="url")
    ask = _tool(cloro, "cloro_ask")(engine="chatgpt", prompt="jane ?")
    if per is not None:
        assert [s["url"] for s in ask["sources"]] == [COMPANY]


# ── tavily ────────────────────────────────────────────────────────────────────

def _tavily():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("k", False))
    cls = patch("oto.tools.tavily.client.TavilyClient")
    return key, cls


def test_tavily_search_drops_profiles(per):
    from oto_mcp.tools import tavily
    key, cls = _tavily()
    with key, cls as c:
        raw = {"answer": "…", "results": _results("url")}
        c.return_value.search.return_value = raw
        out = _tool(tavily, "tavily_search")(query="jane")
    _assert_effect(out, raw, per, path=("results",), key="url")


def test_tavily_extract_refuses_the_whole_batch_naming_the_urls(per):
    from oto_mcp.tools import tavily
    key, cls = _tavily()
    with key, cls as c:
        c.return_value.extract.return_value = {"results": []}
        fn = _tool(tavily, "tavily_extract")
        if per is None:
            fn(urls=[PROFILE, COMPANY])
            c.return_value.extract.assert_called_once()
            return
        with pytest.raises(McpError) as e:
            fn(urls=[COMPANY, PROFILE])
        assert PROFILE in str(e.value)
        c.return_value.extract.assert_not_called()
        fn(urls=[COMPANY])
        c.return_value.extract.assert_called_once()


def test_tavily_map_and_crawl_refuse_the_root_and_filter_pages(per):
    from oto_mcp.tools import tavily
    key, cls = _tavily()
    with key, cls as c:
        raw_map = {"results": [PROFILE, COMPANY]}
        c.return_value.map_site.return_value = raw_map
        c.return_value.crawl.return_value = {"results": _results("url")}
        fmap, fcrawl = _tool(tavily, "tavily_map"), _tool(tavily, "tavily_crawl")
        if per is not None:
            with pytest.raises(McpError):
                fmap(url=PROFILE)
            with pytest.raises(McpError):
                fcrawl(url=PROFILE)
            c.return_value.map_site.assert_not_called()
            c.return_value.crawl.assert_not_called()
        out = fmap(url="https://www.linkedin.com/")
        _assert_effect(out, raw_map, per, path=("results",))
        pages = fcrawl(url="https://www.linkedin.com/")
        if per is not None:
            assert [p["url"] for p in pages["results"]] == [COMPANY]


# ── firecrawl ─────────────────────────────────────────────────────────────────

def _firecrawl():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("k", False))
    cls = patch("oto.tools.firecrawl.client.FirecrawlClient")
    return key, cls


def test_firecrawl_scrape_map_crawl_extract_refuse_excluded_input(per):
    from oto_mcp.tools import firecrawl
    key, cls = _firecrawl()
    with key, cls as c:
        inst = c.return_value
        calls = {"firecrawl_scrape": (inst.scrape, {"url": PROFILE}),
                 "firecrawl_map": (inst.map_site, {"url": PROFILE}),
                 "firecrawl_crawl": (inst.crawl, {"url": PROFILE}),
                 "firecrawl_extract": (inst.extract, {"urls": [COMPANY, PROFILE]})}
        for name, (method, kw) in calls.items():
            method.return_value = {"success": True}
            fn = _tool(firecrawl, name)
            if per is None:
                fn(**kw)
                method.assert_called_once()
                continue
            with pytest.raises(McpError) as e:
                fn(**kw)
            assert "linkedin.com/in/" in str(e.value), name
            method.assert_not_called()


def test_firecrawl_search_and_crawl_status_filter_pages(per):
    from oto_mcp.tools import firecrawl
    key, cls = _firecrawl()
    with key, cls as c:
        raw = {"success": True, "data": {"web": _results("url")}}
        c.return_value.search.return_value = raw
        out = _tool(firecrawl, "firecrawl_search")(query="jane")
        _assert_effect(out, raw, per, path=("data", "web"), key="url")
        c.return_value.crawl_status.return_value = {
            "status": "completed",
            "data": [{"markdown": "a", "metadata": {"sourceURL": PROFILE}},
                     {"markdown": "b", "metadata": {"sourceURL": COMPANY}}]}
        st = _tool(firecrawl, "firecrawl_crawl_status")(job_id="j")
        if per is not None:
            assert [p["metadata"]["sourceURL"] for p in st["data"]] == [COMPANY]
            assert st["excluded_by_perimeter"]["count"] == 1


# ── web_read / browser / file_source ──────────────────────────────────────────

class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco(a[0]) if a and callable(a[0]) else deco


def test_web_read_refuses_before_any_fetch_and_after_a_redirect(monkeypatch, per):
    from oto_mcp.tools import web as W
    fetched = []
    html = "<html><title>t</title><body>" + "<p>texte utile</p>" * 40 + "</body></html>"

    def _fetch(url):
        fetched.append(url)
        return {"ok": True, "status": 200, "html": html,
                "final_url": PROFILE, "verdict": "lu"}
    monkeypatch.setattr(W, "_fetch_http", _fetch)
    reg = _Reg()
    W.register(reg)
    run = lambda **kw: asyncio.run(reg.tools["web_read"](**kw))  # noqa: E731
    if per is None:
        assert run(url=PROFILE)["chemin"] == "http"
        return
    with pytest.raises(McpError):
        run(url=PROFILE)
    assert fetched == []                       # refusé AVANT tout réseau
    # une URL hors périmètre qui REDIRIGE vers un profil : refusée sur l'URL observée
    with pytest.raises(McpError) as e:
        run(url="https://acme.fr/equipe/jane")
    assert PROFILE in str(e.value) and fetched == ["https://acme.fr/equipe/jane"]


def test_browser_fetch_and_eval_refuse_before_browserbase(monkeypatch, per):
    from oto_mcp.tools import browser as B
    calls = []

    async def _fetch(ctx_id, url, as_html=False):
        calls.append(url)
        return {"content": "x", "final_url": url, "status": 200, "title": "t"}

    async def _eval(ctx_id, url, js):
        calls.append(url)
        return {"ok": True}
    monkeypatch.setattr(B.browserbase, "is_configured", lambda: True)
    monkeypatch.setattr(B.browserbase, "fetch_page", _fetch)
    monkeypatch.setattr(B.browserbase, "run_page_eval", _eval)
    monkeypatch.setattr(B, "_context_id", lambda site: "ctx")
    fetch, ev = _tool(B, "browser_fetch"), _tool(B, "browser_eval")
    if per is None:
        assert asyncio.run(fetch(url=PROFILE))["content"] == "x"
        assert asyncio.run(ev(url=PROFILE, js="async () => 1"))["result"] == {"ok": True}
        return
    with pytest.raises(McpError):
        asyncio.run(fetch(url=PROFILE))
    with pytest.raises(McpError):
        asyncio.run(ev(url=PROFILE, js="async () => 1"))
    assert calls == []
    assert asyncio.run(fetch(url=COMPANY))["content"] == "x"


def test_file_source_url_refuses_an_excluded_page(monkeypatch, per):
    from oto_mcp import file_source as fs
    import httpx
    monkeypatch.setattr(fs, "_assert_public_host", lambda host: None)
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("réseau touché")))
    if per is None:
        with pytest.raises(AssertionError):      # sans périmètre on va jusqu'au réseau
            fs.resolve({"kind": "url", "url": PROFILE})
        return
    with pytest.raises(McpError) as e:
        fs.resolve({"kind": "url", "url": PROFILE})
    assert "linkedin.com/in/" in str(e.value)
