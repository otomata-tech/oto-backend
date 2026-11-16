"""Dispatch `engine=` du tool `searchapi_search` (ADR 0047 §Amendement, appliqué au
connecteur searchapi : 7 tools → 1).

Le client vit dans oto-core depuis v1.233.0 (`oto.tools.searchapi.SearchApiClient`,
oto-now/connectors#244) ; ce test l'exerce À TRAVERS l'outil, transport stubbé au
niveau HTTP : une verticale mal câblée partirait chez SearchApi avec le mauvais
`engine` — réponse 200, résultats plausibles, aucune erreur. D'où : pour CHAQUE
verticale d'avant, l'`engine` et les params RÉELLEMENT envoyés amont, plus les refus
(moteur vide, requête absente, en `INVALID_PARAMS`), l'invariant « la clé ne passe
jamais en query » et le comptage sous clé plateforme, qui restent à l'hôte.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.types import INVALID_PARAMS

from oto_mcp.mcp_errors import McpError


def _tool(name: str = "searchapi_search"):
    from fastmcp import FastMCP
    from oto_mcp.tools import searchapi as S

    m = FastMCP("t")
    S.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _tool_names():
    from fastmcp import FastMCP
    from oto_mcp.tools import searchapi as S

    m = FastMCP("t")
    S.register(m)
    return {t.name for t in asyncio.run(m._list_tools())}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code
        self.text, self.headers = "", {}

    def json(self):
        return self._payload


@pytest.fixture
def seen(monkeypatch):
    """Capture le GET amont (url, params, headers) + la clé résolue.

    On intercepte au niveau HTTP et pas au niveau d'un client stubé : c'est la
    construction du payload (engine + noms de params SearchApi) qui est le contrat.
    """
    import requests

    from oto_mcp.tools import searchapi as S

    captured: dict = {"usage": []}

    def _get(self, url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp({"organic_results": []})

    monkeypatch.setattr(requests.Session, "get", _get)
    monkeypatch.setattr(S.access, "resolve_api_key", lambda p: ("k-1", False))
    from _releve import capter_units
    capter_units(monkeypatch, "searchapi", lambda p, n: captured["usage"].append(p))
    return captured


# --- une verticale = un `engine`, et les params SearchApi qui vont avec ---------
# Chaque cas rejoue à l'identique l'un des 6 tools typés supprimés : mêmes arguments
# d'entrée, même payload attendu amont.

@pytest.mark.parametrize("kwargs,expected", [
    # ex-searchapi_web_search
    ({"engine": "google", "query": "pizza", "country": "fr", "language": "fr",
      "location": "Paris, France", "num": 20, "page": 2},
     {"engine": "google", "q": "pizza", "gl": "fr", "hl": "fr",
      "location": "Paris, France", "num": 20, "page": 2}),
    # ex-searchapi_news_search
    ({"engine": "google_news", "query": "otomata", "country": "fr", "language": "fr"},
     {"engine": "google_news", "q": "otomata", "gl": "fr", "hl": "fr"}),
    # ex-searchapi_jobs_search
    ({"engine": "google_jobs", "query": "data engineer",
      "location": "Paris, France", "country": "fr", "language": "fr"},
     {"engine": "google_jobs", "q": "data engineer", "location": "Paris, France",
      "gl": "fr", "hl": "fr"}),
    # ex-searchapi_scholar_search
    ({"engine": "google_scholar", "query": "graph neural network", "language": "en"},
     {"engine": "google_scholar", "q": "graph neural network", "hl": "en"}),
    # ex-searchapi_maps_search
    ({"engine": "google_maps", "query": "coffee shops",
      "location": "Paris, France", "language": "fr"},
     {"engine": "google_maps", "q": "coffee shops", "location": "Paris, France",
      "hl": "fr"}),
    # ex-searchapi_youtube_search
    ({"engine": "youtube", "query": "fastmcp", "country": "us", "language": "en"},
     {"engine": "youtube", "q": "fastmcp", "gl": "us", "hl": "en"}),
])
def test_each_vertical_sends_its_engine_and_params(seen, kwargs, expected):
    _tool()(**kwargs)
    assert seen["params"] == expected
    assert seen["url"] == "https://www.searchapi.io/api/v1/search"


def test_absent_fields_are_not_sent(seen):
    """Un champ non fourni ne doit pas partir à `None` : SearchApi le lirait comme
    une valeur (param inconnu → 4xx, ou filtre vide silencieux)."""
    _tool()(engine="google_news", query="otomata")
    assert seen["params"] == {"engine": "google_news", "q": "otomata"}


# --- passerelle générique (ex-searchapi_search) ---------------------------------

def test_arbitrary_engine_with_raw_params(seen):
    """La capacité centrale du connecteur : atteindre un moteur SANS champ typé,
    y compris un moteur que ce module ne nomme pas."""
    _tool()(engine="google_lens", params={"url": "https://x/y.png"})
    assert seen["params"] == {"engine": "google_lens", "url": "https://x/y.png"}


def test_unknown_engine_is_not_rejected(seen):
    """`engine` est OUVERT par contrat : une allowlist fermerait la capacité (un
    moteur ajouté par SearchApi deviendrait inatteignable sans redéploiement)."""
    _tool()(engine="engine_qui_nexiste_pas_encore", params={"q": "x"})
    assert seen["params"]["engine"] == "engine_qui_nexiste_pas_encore"


def test_params_is_merged_last_and_wins(seen):
    """Règle de précédence documentée : `params` est l'échappatoire brute, une clé
    qui double un champ typé l'emporte (sinon deux façons d'écrire `q` = ambiguïté)."""
    _tool()(engine="google", query="typed", country="fr",
            params={"q": "raw", "time_period": "last_week"})
    assert seen["params"] == {"engine": "google", "q": "raw", "gl": "fr",
                              "time_period": "last_week"}


def test_typed_fields_and_params_compose(seen):
    _tool()(engine="google_shopping", query="clavier", country="fr",
            params={"sort_by": "price_low_to_high"})
    assert seen["params"] == {"engine": "google_shopping", "q": "clavier",
                              "gl": "fr", "sort_by": "price_low_to_high"}


# --- refus ----------------------------------------------------------------------

@pytest.mark.parametrize("engine", ["", "   "])
def test_blank_engine_is_refused_naming_the_common_engines(seen, engine):
    """Pas de verticale par défaut : sans `engine` explicite l'appel partirait sur un
    moteur deviné. Le message doit nommer les ids courants."""
    with pytest.raises(McpError, match="engine"):
        _tool()(engine=engine, query="pizza")
    assert "params" not in seen


def test_blank_engine_message_lists_common_ids(seen):
    with pytest.raises(McpError) as e:
        _tool()(engine="", query="pizza")
    msg = str(e.value)
    assert e.value.error.code == INVALID_PARAMS
    for eid in ("google", "google_news", "google_jobs", "google_maps", "youtube"):
        assert eid in msg


def test_missing_query_and_params_is_refused(seen):
    """Ni `query` ni `params` = une requête vide envoyée à SearchApi : refus
    actionnable, jamais un appel amont qui échouera plus loin avec un message opaque."""
    with pytest.raises(McpError, match="query") as e:
        _tool()(engine="google")
    assert e.value.error.code == INVALID_PARAMS
    assert "params" not in seen


def test_missing_query_message_points_to_the_params_escape_hatch(seen):
    with pytest.raises(McpError) as e:
        _tool()(engine="google_lens")
    assert "params" in str(e.value)
    assert "google_lens" in str(e.value)


def test_params_alone_is_enough(seen):
    """`params` seul suffit : les moteurs dont l'entrée n'est pas `q` doivent rester
    atteignables (c'était l'ex-tool générique)."""
    _tool()(engine="google_flights", params={"departure_id": "CDG",
                                             "arrival_id": "JFK"})
    assert seen["params"]["departure_id"] == "CDG"


# --- clé & comptage plateforme ---------------------------------------------------

def test_key_travels_in_the_authorization_header_never_in_query(seen):
    """Invariant du module : la clé passe en `Authorization: Bearer`, jamais en query
    (elle fuirait dans les logs d'accès)."""
    _tool()(engine="google", query="pizza")
    assert seen["headers"] == {"Authorization": "Bearer k-1"}
    assert "api_key" not in seen["params"]
    assert "k-1" not in str(seen["params"])


def test_platform_usage_is_counted_only_on_the_platform_key(seen, monkeypatch):
    """Le quota daily des members repose sur ce comptage : un appel servi par la clé
    plateforme DOIT être compté, un appel BYO ne doit pas l'être."""
    from oto_mcp.tools import searchapi as S

    _tool()(engine="google", query="pizza")
    assert seen["usage"] == []

    monkeypatch.setattr(S.access, "resolve_api_key", lambda p: ("k-plat", True))
    _tool()(engine="google", query="pizza")
    assert seen["usage"] == ["searchapi"]


def test_upstream_error_is_propagated(seen, monkeypatch):
    """Un 4xx amont (input rejeté) remonte tel quel (`UpstreamHTTPError`, statut
    compris) — il n'est ni avalé ni converti en résultat vide, ni compté."""
    import requests
    from oto.tools.common.errors import UpstreamHTTPError

    from oto_mcp.tools import searchapi as S

    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, **k: _Resp({"error": "bad"}, status_code=400))
    monkeypatch.setattr(S.access, "resolve_api_key", lambda p: ("k-plat", True))
    with pytest.raises(UpstreamHTTPError) as e:
        _tool()(engine="google", query="pizza")
    assert e.value.status_code == 400
    assert seen["usage"] == []


# --- surface ----------------------------------------------------------------------

def test_the_connector_exposes_exactly_one_tool():
    """La consolidation elle-même : un endpoint SearchApi ⟹ un tool. Un tool typé
    ré-ajouté par mégarde (ou un reste de la surface d'avant) casse ici."""
    assert _tool_names() == {"searchapi_search"}
