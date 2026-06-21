"""SerpApi — recherche multi-moteurs (scope complet de l'API SerpApi).

Wrappe `oto.tools.serpapi.SerpAPIClient`. Un tool générique `serpapi_search`
atteint **n'importe quel moteur** SerpApi (tous les verticaux Google + Bing,
DuckDuckGo, Yahoo, Baidu, Yandex, YouTube, Walmart, Amazon, eBay, Home Depot,
Apple App Store, Yelp, Naver, TripAdvisor, Brave, google_trends/finance/flights/
hotels/events/play…). Des tools typés couvrent les verticaux phares que Serper
n'expose pas (trends, finance, flights, hotels, events, youtube, walmart, amazon,
ebay, bing) + le sourcing d'offres (Google Jobs).

Clé résolue par appel via `access.resolve_api_key("serpapi")` : user key
(`/account`) ou credential partagé de l'org si posé, sinon clé plateforme + quota
daily pour les members. Pourquoi SerpApi en plus de Serper : SerpApi a des moteurs
dédiés que Serper n'a pas (jobs, trends, finance, flights, hotels, marketplaces…).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.serpapi.client import SerpAPIClient

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

    # --- générique : tout le scope ------------------------------------------
    @mcp.tool()
    async def serpapi_search(
        engine: str,
        params: Optional[dict] = None,
        max_results: Optional[int] = None,
        results_key: Optional[str] = None,
    ) -> dict:
        """Generic SerpApi call — reach ANY SerpApi engine.

        Use this for engines without a dedicated tool below.

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
            params: engine-specific params, e.g. {"q": "pizza", "gl": "us", "hl": "en"}.
                See serpapi.com docs for each engine's parameters.
            max_results: with `results_key`, auto-paginate up to this many.
            results_key: result array to paginate/cap (e.g. "organic_results").

        Returns the raw SerpApi JSON payload.
        """
        return _run("search", engine=engine, params=params or {},
                    max_results=max_results, results_key=results_key)

    # --- tools typés phares (complètent Serper) -----------------------------
    @mcp.tool()
    async def serpapi_bing_search(
        query: str,
        country: Optional[str] = None,
        language: Optional[str] = None,
        count: int = 10,
    ) -> dict:
        """Bing web search via SerpApi. Returns 'organic_results'.

        Args:
            query: search query (Bing `q`).
            country: market/country code (Bing `cc`, e.g. "us", "fr").
            language: UI language (Bing `setlang`).
            count: number of results.
        """
        params: dict = {"q": query, "count": count}
        if country:
            params["cc"] = country
        if language:
            params["setlang"] = language
        return _run("search", engine="bing", params=params)

    @mcp.tool()
    async def serpapi_youtube_search(query: str, language: Optional[str] = None,
                                     country: Optional[str] = None) -> dict:
        """YouTube search via SerpApi — videos, channels, playlists for a query.

        Args:
            query: search query (YouTube `search_query`).
            language: interface language (`hl`).
            country: country code (`gl`).
        """
        params: dict = {"search_query": query}
        if language:
            params["hl"] = language
        if country:
            params["gl"] = country
        return _run("search", engine="youtube", params=params)

    @mcp.tool()
    async def serpapi_walmart_search(query: str, page: int = 1) -> dict:
        """Walmart product search via SerpApi. Returns 'organic_results' (products
        with price, rating, seller).

        Args:
            query: product query (Walmart `query`).
            page: result page.
        """
        return _run("search", engine="walmart", params={"query": query, "page": page})

    @mcp.tool()
    async def serpapi_amazon_search(query: str, domain: str = "amazon.com",
                                    page: int = 1) -> dict:
        """Amazon product search via SerpApi. Returns 'organic_results' (products,
        price, rating, ASIN).

        Args:
            query: product query (Amazon `k`).
            domain: Amazon domain, e.g. "amazon.com", "amazon.fr".
            page: result page.
        """
        return _run("search", engine="amazon",
                    params={"k": query, "amazon_domain": domain, "page": page})

    @mcp.tool()
    async def serpapi_ebay_search(query: str, domain: str = "ebay.com",
                                  page: int = 1) -> dict:
        """eBay product search via SerpApi. Returns 'organic_results' (listings,
        price, condition, shipping).

        Args:
            query: search query (eBay `_nkw`).
            domain: eBay domain, e.g. "ebay.com", "ebay.fr".
            page: result page (`_pgn`).
        """
        return _run("search", engine="ebay",
                    params={"_nkw": query, "ebay_domain": domain, "_pgn": page})

    @mcp.tool()
    async def serpapi_google_trends(
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
    async def serpapi_google_finance(query: str, window: Optional[str] = None) -> dict:
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
    async def serpapi_google_flights(
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
    async def serpapi_google_hotels(
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

    @mcp.tool()
    async def serpapi_google_events(query: str, location: Optional[str] = None,
                                    language: Optional[str] = None,
                                    country: Optional[str] = None) -> dict:
        """Google Events via SerpApi — local/online events for a query.

        Args:
            query: events query, e.g. "tech conferences in Paris" (Events `q`).
            location: geographic location (e.g. "Paris, France").
            language: interface language (`hl`).
            country: country code (`gl`).
        """
        params: dict = {"q": query}
        if location:
            params["location"] = location
        if language:
            params["hl"] = language
        if country:
            params["gl"] = country
        return _run("search", engine="google_events", params=params)

    # --- jobs (existant, recâblé sur _run) ----------------------------------
    @mcp.tool()
    async def serpapi_search_jobs(
        query: Optional[str] = None,
        company: Optional[str] = None,
        location: Optional[str] = None,
        country: Optional[str] = None,
        language: str = "en",
        max_results: int = 50,
        no_cache: bool = False,
    ) -> dict:
        """Search Google Jobs for live job postings (job-board sourcing).

        Args:
            query: free-text job query, e.g. "data engineer Paris", "senior
                python remote". Preferred for general sourcing.
            company: shortcut — if `query` is omitted, searches "<company> jobs".
            location: e.g. "Paris, France".
            country: 2-letter code, e.g. "fr", "us" (Google `gl`).
            language: language code (Google `hl`).
            max_results: max postings (pagination handled).
            no_cache: force fresh results.

        Returns the SerpApi payload incl. `jobs_results` (each with a `job_id`
        usable in serpapi_job_details).
        """
        return _run(
            "search_jobs", query=query, company=company, location=location,
            country=country, language=language, max_results=max_results,
            no_cache=no_cache)

    @mcp.tool()
    async def serpapi_job_details(job_id: str) -> dict:
        """Fetch the full detail of one job posting by its Google Jobs `job_id`
        (apply options, full description) — `job_id` comes from serpapi_search_jobs."""
        return _run("get_job_details", job_id=job_id)
