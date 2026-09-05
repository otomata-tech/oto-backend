"""SerpApi — recherche multi-moteurs (scope complet de l'API SerpApi).

Wrappe `oto.tools.serpapi.SerpAPIClient`. Un tool générique `serpapi_search`
atteint **n'importe quel moteur** SerpApi (tous les verticaux Google + Bing,
DuckDuckGo, Yahoo, Baidu, Yandex, YouTube, Walmart, Amazon, eBay, Home Depot,
Apple App Store, Yelp, Naver, TripAdvisor, Brave, google_trends/finance/flights/
hotels/events/play…).

**Surface consolidée (ADR 0047 §Amendement appliqué à un connecteur)** : le
moteur est un **axe `engine=`**, pas un tool par moteur — bing / youtube /
walmart / amazon / ebay / google_events partagent exactement les mêmes
paramètres (`query`, `country`, `language`, `location`, `page`, `count`,
`domain`), seuls leurs NOMS natifs diffèrent, et `serpapi_search` les traduit.
`serpapi_jobs` porte l'objet métier « offre d'emploi » (op=search|details).
Restent des tools à part les verticaux dont le contrat n'est PAS une recherche
par mot-clé — leurs paramètres ne recouvrent pas ceux des autres :
`serpapi_google_trends` (data_type/date), `serpapi_google_finance` (window),
`serpapi_google_flights` (departure_id/arrival_id/dates, aucun `query`),
`serpapi_google_hotels` (check_in/check_out/adults).

⚠️ **Un résultat VIDE n'est jamais resservi par un cache** (signal d'usage #456,
2026-08-27) : SerpApi mémorisait une heure durant les réponses à zéro résultat —
et le cache d'arête Cloudflare devant lui aussi — ce qui transformait une absence
momentanée en absence PERMANENTE et fausse. Sur un connecteur qui sert
d'indicateur d'activité (« une maison qui recrute se développe »), un zéro figé
est indiscernable d'une vraie absence, et une campagne qui traite chaque ligne une
seule fois n'a aucun moyen de le rattraper. La garantie est portée par le client
oto-core (`_empty_must_be_fresh`) : un vide périmé est refait en forçant
`no_cache`, un NON-vide garde le droit au cache. Elle vaut partout où le tableau
de résultats est nommé — toujours pour `serpapi_jobs`, sur `results_key=` pour
`serpapi_search`.

Clé résolue par appel via `access.resolve_api_key("serpapi")` : user key
(`/account`) ou credential partagé de l'org si posé, sinon clé plateforme + quota
daily pour les members. Pourquoi SerpApi en plus de Serper : SerpApi a des moteurs
dédiés que Serper n'a pas (jobs, trends, finance, flights, hotels, marketplaces…).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

import datetime as _dt

from .. import access, url_perimeter
from ..connectors import verify as connector_verify

_ACCOUNT_URL = "https://serpapi.com/account"


def _verify(fields: dict, config: dict | None = None) -> dict:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth+quota`.

    `GET https://serpapi.com/account`. Ce que la doc SerpApi établit, cité :

    - **authentifié** — la clé (`api_key`) est un paramètre requis ;
    - **sans effet de bord** — une lecture d'information de compte ;
    - **gratuit, et là c'est ÉCRIT** — « Account API is free of charge, and using
      it will not be counted toward your monthly quota. » Comme Hunter, pas
      besoin de se contenter de l'absence de compteur comme indice.

    Le solde vient au même appel : `total_searches_left` (le plan mensuel restant
    PLUS les crédits ponctuels — c'est le nombre qui dit si un appel de recherche
    passera, pas seulement le quota du plan). Compte à sec → `QuotaEpuise`, verdict
    `no_quota`, conduite « recharge » — pas « reconnecte », qui n'y changerait rien.

    Ne fabrique pas de solde s'il n'est pas lisible : mieux vaut n'en rendre aucun
    que d'en inventer un si SerpApi changeait la forme de sa réponse.
    """
    import requests

    r = requests.get(_ACCOUNT_URL, params={"api_key": fields["key"]}, timeout=15)
    r.raise_for_status()
    infos = r.json() or {}
    if not infos.get("account_id"):
        raise RuntimeError(
            "SerpApi a répondu sans identifier de compte pour cette clé — "
            f"réponse inattendue : {str(infos)[:200]}")
    restant = infos.get("total_searches_left")
    if isinstance(restant, int):
        if restant <= 0:
            raise connector_verify.QuotaEpuise(
                f"La clé SerpApi est bonne, mais le compte est à sec (0 recherche "
                "restante). Recharge le compte chez SerpApi — reconnecter n'y "
                "changerait rien.")
        return {"quota": {
            "restant": restant,
            "unite": "recherches",
            "mesure_a": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        }}
    return {}

# --- traduction des params partagés vers le nom natif de chaque moteur --------
# Seuls les moteurs qui DIVERGENT de la convention Google/SerpApi sont listés ;
# tout moteur absent utilise `_DEFAULT_PARAMS` (q / gl / hl / location), ce qui
# couvre google_events et l'ensemble des verticaux google.
_ENGINE_PARAMS: dict[str, dict[str, str]] = {
    "bing": {"query": "q", "country": "cc", "language": "setlang", "count": "count"},
    "youtube": {"query": "search_query", "country": "gl", "language": "hl"},
    "walmart": {"query": "query", "page": "page"},
    "amazon": {"query": "k", "domain": "amazon_domain", "page": "page"},
    "ebay": {"query": "_nkw", "domain": "ebay_domain", "page": "_pgn"},
}
_DEFAULT_PARAMS: dict[str, str] = {
    "query": "q", "country": "gl", "language": "hl", "location": "location",
}
# Défauts historiques des ex-tools typés — conservés à l'identique par moteur.
_ENGINE_DEFAULTS: dict[str, dict] = {
    "bing": {"count": 10},
    "walmart": {"page": 1},
    "amazon": {"domain": "amazon.com", "page": 1},
    "ebay": {"domain": "ebay.com", "page": 1},
}
_SHARED_ORDER = ("query", "country", "language", "location", "page", "count", "domain")


def register(mcp: FastMCP) -> None:
    from oto.tools.serpapi.client import SerpAPIClient

    connector_verify.register("serpapi", _verify, couvre=connector_verify.AUTH_QUOTA)

    def _client() -> tuple[SerpAPIClient, bool]:
        key, is_platform = access.resolve_api_key("serpapi")
        return SerpAPIClient(api_key=key), is_platform

    def _run(method: str, **kwargs) -> dict:
        """Résout la clé, appelle la méthode du client, compte l'usage plateforme."""
        client, is_platform = _client()
        result = getattr(client, method)(**kwargs)
        if is_platform:
            access.record_platform_usage("serpapi")
        return result

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _need(value, name: str, op: str):
        """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback."""
        if value is None:
            raise _bad(f"op='{op}' requiert {name}")
        return value

    def _shared_params(engine: str, **supplied) -> dict:
        """Traduit les arguments partagés dans les noms natifs de `engine`.

        Un argument sans équivalent connu pour ce moteur est REFUSÉ (jamais
        envoyé sous un nom deviné : un filtre mal nommé serait ignoré en
        silence par SerpApi et rendrait un résultat faux sans erreur).
        """
        spec = _ENGINE_PARAMS.get(engine, _DEFAULT_PARAMS)
        defaults = _ENGINE_DEFAULTS.get(engine, {})
        out: dict = {}
        for name in _SHARED_ORDER:
            value = supplied.get(name)
            if value is None:
                value = defaults.get(name)
            if value is None:
                continue
            native = spec.get(name)
            if native is None:
                raise _bad(
                    f"`{name}` n'a pas d'équivalent connu pour engine='{engine}' — "
                    f"passe-le dans `params` sous le nom attendu par ce moteur "
                    f"(voir serpapi.com).")
            out[native] = value
        return out

    # --- recherche : tout le scope, un moteur par appel ----------------------
    @mcp.tool()
    def serpapi_search(
        engine: str,
        query: Optional[str] = None,
        country: Optional[str] = None,
        language: Optional[str] = None,
        location: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
        domain: Optional[str] = None,
        params: Optional[dict] = None,
        max_results: Optional[int] = None,
        results_key: Optional[str] = None,
    ) -> dict:
        """SerpApi search — reach ANY SerpApi engine through the `engine=` axis.

        The shared arguments (`query`, `country`, `language`, `location`, `page`,
        `count`, `domain`) are translated into each engine's own parameter names;
        `params` stays the raw escape hatch for anything else (and for engines
        with no shortcut). Returns the raw SerpApi JSON payload. Under a project
        with `excluded_url_prefixes`, matching results are dropped and counted.

        Engines with a mapped shortcut (native `query` name in parentheses):

        - **bing** (`q`) — Bing web search. Returns 'organic_results'.
          `country` = market/country code (Bing `cc`, e.g. "us", "fr"),
          `language` = UI language (Bing `setlang`), `count` = number of results
          (default 10).
        - **youtube** (`search_query`) — YouTube search: videos, channels,
          playlists for a query. `language` = interface language (`hl`),
          `country` = country code (`gl`).
        - **walmart** (`query`) — Walmart product search. Returns
          'organic_results' (products with price, rating, seller). `page` =
          result page (default 1).
        - **amazon** (`k`) — Amazon product search. Returns 'organic_results'
          (products, price, rating, ASIN). `domain` = Amazon domain, e.g.
          "amazon.com", "amazon.fr" (default "amazon.com"); `page` = result page.
        - **ebay** (`_nkw`) — eBay product search. Returns 'organic_results'
          (listings, price, condition, shipping). `domain` = eBay domain, e.g.
          "ebay.com", "ebay.fr" (default "ebay.com"); `page` = result page
          (eBay `_pgn`).
        - **google_events** (`q`) — Google Events: local/online events for a
          query, e.g. "tech conferences in Paris". `location` = geographic
          location (e.g. "Paris, France"), `language` = interface language
          (`hl`), `country` = country code (`gl`).
        - **any other engine** — Google/SerpApi convention: `query`→`q`,
          `country`→`gl`, `language`→`hl`, `location`→`location`.
          `page`/`count`/`domain` have no portable equivalent: on an engine
          without a shortcut they are REFUSED rather than silently sent under a
          guessed name — pass them in `params` under the name that engine expects.

        Dedicated tools cover the verticals whose contract is NOT a keyword
        search: `serpapi_jobs` (Google Jobs), `serpapi_google_trends`,
        `serpapi_google_finance`, `serpapi_google_flights`,
        `serpapi_google_hotels`. They stay reachable here via `engine=` + `params=`.

        Args:
            engine: SerpApi engine id. Common values —
                Google verticals: google, google_images, google_news, google_maps,
                google_local, google_videos, google_shopping, google_scholar,
                google_patents, google_lens, google_autocomplete, google_trends,
                google_finance, google_flights, google_hotels, google_events,
                google_play, google_jobs, google_reverse_image, google_ai_overview.
                Other engines: bing, duckduckgo, yahoo, baidu, yandex, brave,
                youtube, ebay, walmart, amazon, home_depot, apple_app_store, yelp,
                naver, tripadvisor.
            query: search query, mapped to the engine's own query parameter.
            country: country/market restriction, mapped per engine.
            language: interface/UI language, mapped per engine.
            location: geographic location (e.g. "Paris, France").
            page: result page — walmart, amazon, ebay.
            count: number of results — bing.
            domain: marketplace domain — amazon, ebay.
            params: engine-specific params, e.g. {"q": "pizza", "gl": "us", "hl": "en"}.
                See serpapi.com docs for each engine's parameters. Merged LAST:
                a key given here overrides the value derived from the arguments above.
            max_results: with `results_key`, auto-paginate up to this many. No-op
                if the engine returns no `serpapi_pagination.next_page_token`.
            results_key: result array to paginate/cap (e.g. "organic_results").
                Naming it also buys the empty-result freshness guarantee: an
                empty array is then re-issued rather than served from cache, and
                the payload gains an `oto_freshness` block (`age_seconds`,
                `refetched`). Without it the payload passes through untouched —
                the client will not guess which array carries the answer.
        """
        payload = _shared_params(
            engine, query=query, country=country, language=language,
            location=location, page=page, count=count, domain=domain)
        payload.update(params or {})
        return url_perimeter.filter_results(
            _run("search", engine=engine, params=payload,
                 max_results=max_results, results_key=results_key),
            url_perimeter.perimeter_of_call())

    # --- offres d'emploi (Google Jobs) --------------------------------------
    @mcp.tool()
    def serpapi_jobs(
        op: Literal["search", "details"] = "search",
        query: Optional[str] = None,
        company: Optional[str] = None,
        location: Optional[str] = None,
        country: Optional[str] = None,
        language: str = "en",
        max_results: int = 50,
        no_cache: bool = False,
        job_id: Optional[str] = None,
    ) -> dict:
        """Google Jobs — live job postings (job-board sourcing).

        `op` :
        - **"search"** (default) : search Google Jobs for live job postings.
          Returns the SerpApi payload incl. `jobs_results` (each with a `job_id`
          usable in op="details") and an `oto_freshness` block — `age_seconds`
          (how old the answer actually is: ~0 = just scraped, a large value = a
          cache served it) and `refetched` (whether a stale empty was re-issued
          for you). **An empty `jobs_results` is always freshly observed**, so a
          zero here can be read as a real absence of postings.
        - **"details"** : fetch the full detail of one job posting by its Google
          Jobs `job_id` (apply options, full description) — `job_id` comes from
          op="search".

        Args:
            op: search (default) | details.
            query: op="search" — free-text job query, e.g. "data engineer Paris",
                "senior python remote". Preferred for general sourcing.
            company: op="search" — shortcut: if `query` is omitted, searches
                "<company> jobs".
            location: op="search" — e.g. "Paris, France".
            country: op="search" — 2-letter code, e.g. "fr", "us" (Google `gl`).
            language: op="search" — language code (Google `hl`).
            max_results: op="search" — max postings (pagination handled).
            no_cache: op="search" — force a fresh scrape even when the answer is
                NOT empty (slower: 5-20 s instead of ~0 s). You do NOT need it to
                trust a zero — an empty result is never served from cache.
            job_id: op="details" — Google Jobs job id, from op="search".
        """
        if op == "search":
            if query is None and company is None:
                raise _bad("op='search' requiert query ou company")
            return _run(
                "search_jobs", query=query, company=company, location=location,
                country=country, language=language, max_results=max_results,
                no_cache=no_cache)

        if op == "details":
            return _run("get_job_details", job_id=_need(job_id, "job_id", op))

        raise _bad("op doit être 'search' ou 'details'")

    # --- verticaux à contrat propre (params disjoints, non fusionnables) -----
    @mcp.tool()
    def serpapi_google_trends(
        query: str,
        data_type: str = "TIMESERIES",
        country: Optional[str] = None,
        date: Optional[str] = None,
    ) -> dict:
        """Google Trends via SerpApi — interest over time / by region for a term.

        Args:
            query: term(s), comma-separated for comparison (Trends `q`).
            data_type: TIMESERIES (interest over time), GEO_MAP (by region),
                RELATED_QUERIES, RELATED_TOPICS.
            country: geo restriction (e.g. "US", "FR"); omit for worldwide.
            date: time range, e.g. "today 12-m", "2021-01-01 2021-12-31".
        """
        params: dict = {"q": query, "data_type": data_type}
        if country:
            params["geo"] = country
        if date:
            params["date"] = date
        return _run("search", engine="google_trends", params=params)

    @mcp.tool()
    def serpapi_google_finance(query: str, window: Optional[str] = None) -> dict:
        """Google Finance via SerpApi — quote/markets for a ticker or symbol.

        Args:
            query: ticker, e.g. "GOOGL:NASDAQ", "BTC-USD" (Finance `q`).
            window: chart range, e.g. "1D", "5D", "1M", "6M", "1Y", "5Y", "MAX".
        """
        params: dict = {"q": query}
        if window:
            params["window"] = window
        return _run("search", engine="google_finance", params=params)

    @mcp.tool()
    def serpapi_google_flights(
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: Optional[str] = None,
        currency: str = "USD",
        country: Optional[str] = None,
    ) -> dict:
        """Google Flights via SerpApi — flight options between two airports.

        Args:
            departure_id: origin airport/city code, e.g. "CDG", "PAR".
            arrival_id: destination code, e.g. "JFK", "NYC".
            outbound_date: YYYY-MM-DD.
            return_date: YYYY-MM-DD for round-trip; omit for one-way (set type=2).
            currency: ISO currency, e.g. "USD", "EUR".
            country: country code (`gl`).
        """
        params: dict = {
            "departure_id": departure_id, "arrival_id": arrival_id,
            "outbound_date": outbound_date, "currency": currency,
        }
        if return_date:
            params["return_date"] = return_date
        else:
            params["type"] = 2  # one-way
        if country:
            params["gl"] = country
        return _run("search", engine="google_flights", params=params)

    @mcp.tool()
    def serpapi_google_hotels(
        query: str,
        check_in_date: str,
        check_out_date: str,
        adults: int = 2,
        currency: str = "USD",
        country: Optional[str] = None,
    ) -> dict:
        """Google Hotels via SerpApi — hotel/property options for a destination.

        Args:
            query: destination/search, e.g. "Paris hotels" (Hotels `q`).
            check_in_date: YYYY-MM-DD.
            check_out_date: YYYY-MM-DD.
            adults: number of adults.
            currency: ISO currency.
            country: country code (`gl`).
        """
        params: dict = {
            "q": query, "check_in_date": check_in_date,
            "check_out_date": check_out_date, "adults": adults, "currency": currency,
        }
        if country:
            params["gl"] = country
        return _run("search", engine="google_hotels", params=params)
