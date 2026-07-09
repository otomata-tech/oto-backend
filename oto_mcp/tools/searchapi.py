"""SearchApi — recherche multi-moteurs via SearchApi.io (scope complet de l'API).

Wrappe l'API REST **SearchApi.io** (`GET https://www.searchapi.io/api/v1/search`,
un seul endpoint paramétré par `engine`). Un tool générique `searchapi_search`
atteint **n'importe quel moteur** SearchApi (verticaux Google, YouTube, Bing,
Baidu, DuckDuckGo, Yandex, Amazon, eBay, Walmart, Google Jobs/News/Maps/Scholar/
Trends/Shopping…). Des tools typés couvrent les verticaux phares (web, news, jobs,
scholar, youtube, maps).

Pas de dépendance oto-core : le client HTTP est **auto-contenu** (httpx), comme
`infosec`/`fr`. Clé résolue par appel via `access.resolve_api_key("searchapi")` :
user key (`/account`) ou credential partagé de l'org si posé, sinon clé plateforme
+ quota daily pour les members (même régime que serper/serpapi). Pourquoi en plus
de serper/serpapi : SearchApi a sa propre couverture de moteurs + parsing, utile
en fallback ou quand une clé SearchApi est déjà en place côté client.
"""
from __future__ import annotations

from typing import Optional

import httpx
from fastmcp import FastMCP

from .. import access

_BASE_URL = "https://www.searchapi.io/api/v1/search"
_TIMEOUT = 30.0


def register(mcp: FastMCP) -> None:

    def _run(engine: str, params: dict) -> dict:
        """Résout la clé, appelle SearchApi, compte l'usage plateforme.

        La clé passe en `Authorization: Bearer` (jamais en query — pas de fuite
        dans les logs d'accès). Un 4xx amont (input rejeté) remonte tel quel via
        `raise_for_status` ; Sentry droppe les 4xx tiers (cf. CLAUDE.md).
        """
        key, is_platform = access.resolve_api_key("searchapi")
        payload = {k: v for k, v in params.items() if v is not None}
        payload["engine"] = engine
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(_BASE_URL, params=payload,
                      headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            data = r.json()
        if is_platform:
            access.record_platform_usage("searchapi")
        return data

    # --- générique : tout le scope ------------------------------------------
    @mcp.tool()
    def searchapi_search(
        engine: str,
        params: Optional[dict] = None,
    ) -> dict:
        """Generic SearchApi.io call — reach ANY SearchApi engine.

        Use this for engines without a dedicated tool below.

        Args:
            engine: SearchApi engine id. Common values —
                Google verticals: google, google_news, google_maps, google_jobs,
                google_scholar, google_images, google_videos, google_shopping,
                google_trends, google_lens, google_autocomplete, google_finance,
                google_play, google_events, google_flights, google_hotels.
                Other engines: youtube, youtube_transcripts, bing, bing_news,
                baidu, duckduckgo, yahoo, yandex, amazon_search, ebay_search,
                walmart_search, apple_app_store.
            params: engine-specific params, e.g. {"q": "pizza", "gl": "us",
                "hl": "en", "location": "Paris, France"}. See searchapi.io docs
                for each engine's parameters.

        Returns the raw SearchApi JSON payload.
        """
        return _run(engine, params or {})

    # --- tools typés phares -------------------------------------------------
    @mcp.tool()
    def searchapi_web_search(
        query: str,
        country: Optional[str] = None,
        language: Optional[str] = None,
        location: Optional[str] = None,
        num: Optional[int] = None,
        page: Optional[int] = None,
    ) -> dict:
        """Google web search via SearchApi. Returns 'organic_results'.

        Args:
            query: search query (Google `q`).
            country: 2-letter country code (Google `gl`, e.g. "fr", "us").
            language: UI language (Google `hl`, e.g. "fr", "en").
            location: geographic location, e.g. "Paris, France".
            num: number of results per page.
            page: result page (1-based).
        """
        return _run("google", {"q": query, "gl": country, "hl": language,
                               "location": location, "num": num, "page": page})

    @mcp.tool()
    def searchapi_news_search(
        query: str,
        country: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict:
        """Google News search via SearchApi — recent news for a query. Returns
        'organic_results' (news articles with source, date, link).

        Args:
            query: news query (`q`).
            country: 2-letter country code (`gl`).
            language: UI language (`hl`).
        """
        return _run("google_news", {"q": query, "gl": country, "hl": language})

    @mcp.tool()
    def searchapi_jobs_search(
        query: str,
        location: Optional[str] = None,
        country: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict:
        """Google Jobs search via SearchApi — live job postings (job-board
        sourcing). Returns 'jobs' (each with title, company, location, apply
        options).

        Args:
            query: free-text job query, e.g. "data engineer Paris" (`q`).
            location: e.g. "Paris, France".
            country: 2-letter country code (`gl`).
            language: language code (`hl`).
        """
        return _run("google_jobs", {"q": query, "location": location,
                                    "gl": country, "hl": language})

    @mcp.tool()
    def searchapi_scholar_search(
        query: str,
        language: Optional[str] = None,
    ) -> dict:
        """Google Scholar search via SearchApi — academic papers/citations for a
        query. Returns 'organic_results' (title, authors, publication, citations).

        Args:
            query: search query (`q`).
            language: UI language (`hl`).
        """
        return _run("google_scholar", {"q": query, "hl": language})

    @mcp.tool()
    def searchapi_maps_search(
        query: str,
        location: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict:
        """Google Maps search via SearchApi — local places/businesses for a query.
        Returns 'local_results' (name, address, phone, rating, coordinates).

        Args:
            query: places query, e.g. "coffee shops" (`q`).
            location: geographic anchor, e.g. "Paris, France".
            language: UI language (`hl`).
        """
        return _run("google_maps", {"q": query, "location": location, "hl": language})

    @mcp.tool()
    def searchapi_youtube_search(
        query: str,
        country: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict:
        """YouTube search via SearchApi — videos, channels, playlists for a query.
        Returns 'videos' / 'channels' / 'playlists'.

        Args:
            query: search query (`q`).
            country: country code (`gl`).
            language: interface language (`hl`).
        """
        return _run("youtube", {"q": query, "gl": country, "hl": language})
