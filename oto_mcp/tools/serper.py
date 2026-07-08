"""Serper — recherche Google (web, images, vidéos, news, places, maps, reviews,
shopping, scholar, patents, lens, autocomplete) + scraping de page.

Clé résolue par appel via `access.resolve_api_key("serper")` : user key
(`/account`) si posée, sinon platform key + quota daily pour les members.
Guests doivent obligatoirement poser leur propre clé.
"""
from __future__ import annotations

import re
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_REQUEST

from .. import access

# Serper renvoie `Serper scrape <status>: <msg>` (RuntimeError nu) quand SON
# scraper n'arrive pas à récupérer une page (JS lourd, anti-bot, page morte) :
# c'est un échec par-URL attendu, pas un bug backend. On le convertit en erreur
# GÉRÉE côté tool (message actionnable pour l'agent + non reporté à Sentry — la
# taxonomie droppe les McpError d'entrée). Les 4xx Serper (crédits/clé) restent
# propagés tels quels : ce sont de vrais problèmes de config, pas des échecs d'URL.
_SCRAPE_STATUS = re.compile(r"Serper scrape (\d{3}):")


def register(mcp: FastMCP) -> None:
    # Import au register pour fail-fast si le package n'est pas installé.
    from oto.tools.serper import SerperClient

    def _client() -> tuple[SerperClient, bool]:
        key, is_platform = access.resolve_api_key("serper")
        return SerperClient(api_key=key), is_platform

    def _run(method: str, **kwargs) -> dict:
        """Résout la clé, appelle la méthode du client, compte l'usage plateforme."""
        client, is_platform = _client()
        result = getattr(client, method)(**kwargs)
        if is_platform:
            access.record_platform_usage("serper")
        return result

    @mcp.tool()
    def serper_web_search(
        query: str,
        num: int = 10,
        page: int = 1,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
        site_filter: Optional[str] = None,
        tbs: Optional[str] = None,
        location: Optional[str] = None,
        autocorrect: Optional[bool] = None,
    ) -> dict:
        """Google web search via Serper.

        Args:
            query: Search query.
            num: Number of results (max 100).
            page: Result page (1-based).
            country: Country code (default "fr").
            language: Language code (default "fr").
            site_filter: Restrict to a domain (e.g. "linkedin.com/in").
            tbs: Google time filter (e.g. "qdr:d" past day, "qdr:w" past week).
            location: Geographic location bias (e.g. "Paris, France").
            autocorrect: Toggle Google spelling autocorrection (default Serper-side).
        """
        return _run(
            "search", query=query, num=num, page=page, country=country,
            language=language, site_filter=site_filter, tbs=tbs,
            location=location, autocorrect=autocorrect,
        )

    @mcp.tool()
    def serper_news_search(
        query: str,
        num: int = 10,
        page: int = 1,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
        tbs: Optional[str] = None,
    ) -> dict:
        """Google News search via Serper.

        Useful for monitoring signals on a target company (PR, hiring, fundraising).

        Args:
            query: Search query.
            num: Number of results (max 100).
            page: Result page (1-based).
            country: Country code (default "fr").
            language: Language code (default "fr").
            tbs: Google time filter (e.g. "qdr:w" past week).
        """
        return _run(
            "search_news", query=query, num=num, page=page, country=country,
            language=language, tbs=tbs,
        )

    @mcp.tool()
    def serper_image_search(
        query: str,
        num: int = 10,
        page: int = 1,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
        tbs: Optional[str] = None,
    ) -> dict:
        """Google Images search via Serper.

        Returns an 'images' array (title, imageUrl, source link, dimensions).
        """
        return _run(
            "search_images", query=query, num=num, page=page, country=country,
            language=language, tbs=tbs,
        )

    @mcp.tool()
    def serper_video_search(
        query: str,
        num: int = 10,
        page: int = 1,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
        tbs: Optional[str] = None,
    ) -> dict:
        """Google Videos search via Serper.

        Returns a 'videos' array (title, link, source, channel, duration, date).
        """
        return _run(
            "search_videos", query=query, num=num, page=page, country=country,
            language=language, tbs=tbs,
        )

    @mcp.tool()
    def serper_places_search(
        query: str,
        num: int = 10,
        page: int = 1,
        location: Optional[str] = None,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google Local / Places search via Serper — businesses for a query.

        Great for local B2B prospecting: returns a 'places' array with title,
        address, phone, website, rating, reviews count and `cid` (usable with
        serper_reviews / serper_maps).

        Args:
            query: What to look for (e.g. "agence immobilière Lyon").
            num: Number of results (max 100).
            page: Result page (1-based).
            location: Geographic location (e.g. "Lyon, France").
            country: Country code (default "fr").
            language: Language code (default "fr").
        """
        return _run(
            "search_places", query=query, num=num, page=page,
            location=location, country=country, language=language,
        )

    @mcp.tool()
    def serper_maps_search(
        query: Optional[str] = None,
        ll: Optional[str] = None,
        place_id: Optional[str] = None,
        cid: Optional[str] = None,
        num: int = 10,
        page: int = 1,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google Maps search via Serper — richer than places, geo-anchored.

        Args:
            query: Search query (e.g. "coffee shops").
            ll: Lat/long + zoom anchor "@lat,lng,zoom" (e.g. "@45.76,4.83,12z").
            place_id: Google place id to look up directly.
            cid: Google customer id of a place.
            num: Number of results (max 100).
            page: Result page (1-based).
            country: Country code (default "fr").
            language: Language code (default "fr").
        """
        return _run(
            "search_maps", query=query, ll=ll, place_id=place_id, cid=cid,
            num=num, page=page, country=country, language=language,
        )

    @mcp.tool()
    def serper_reviews(
        cid: Optional[str] = None,
        fid: Optional[str] = None,
        place_id: Optional[str] = None,
        query: Optional[str] = None,
        sort_by: Optional[str] = None,
        topic_id: Optional[str] = None,
        next_page_token: Optional[str] = None,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google reviews of a place via Serper.

        Identify the place by one of `cid` / `fid` / `place_id` (from a
        serper_places_search / serper_maps_search result) or by free-text `query`.

        Args:
            cid: Google customer id of the place.
            fid: Google feature id of the place.
            place_id: Google place id.
            query: Free-text place lookup (alternative to ids).
            sort_by: 'mostRelevant' | 'newest' | 'highestRating' | 'lowestRating'.
            topic_id: Filter reviews by topic id.
            next_page_token: Pagination cursor from a previous response.
            country: Country code (default "fr").
            language: Language code (default "fr").
        """
        return _run(
            "search_reviews", cid=cid, fid=fid, place_id=place_id, query=query,
            sort_by=sort_by, topic_id=topic_id, next_page_token=next_page_token,
            country=country, language=language,
        )

    @mcp.tool()
    def serper_shopping_search(
        query: str,
        num: int = 10,
        page: int = 1,
        location: Optional[str] = None,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google Shopping search via Serper.

        Returns a 'shopping' array (title, price, source, rating, delivery).
        """
        return _run(
            "search_shopping", query=query, num=num, page=page,
            location=location, country=country, language=language,
        )

    @mcp.tool()
    def serper_scholar_search(
        query: str,
        num: int = 10,
        page: int = 1,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google Scholar search via Serper — academic papers.

        Returns an 'organic' array (title, publication info, year, citedBy, pdf).
        """
        return _run(
            "search_scholar", query=query, num=num, page=page,
            country=country, language=language,
        )

    @mcp.tool()
    def serper_patents_search(
        query: str,
        num: int = 10,
        page: int = 1,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google Patents search via Serper.

        Returns patents with title, inventor, assignee, publication number, dates.
        """
        return _run(
            "search_patents", query=query, num=num, page=page,
            country=country, language=language,
        )

    @mcp.tool()
    def serper_lens(
        url: str,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google Lens via Serper — reverse image search from an image URL.

        Args:
            url: Public URL of the image to analyse.
            country: Country code (default "fr").
            language: Language code (default "fr").
        """
        return _run("search_lens", url=url, country=country, language=language)

    @mcp.tool()
    def serper_autocomplete(
        query: str,
        country: Optional[str] = "fr",
        language: Optional[str] = "fr",
    ) -> dict:
        """Google autocomplete suggestions via Serper.

        Returns a 'suggestions' array — useful for query expansion / keyword ideas.
        """
        return _run(
            "autocomplete", query=query, country=country, language=language,
        )

    @mcp.tool()
    def serper_scrape(url: str, include_markdown: bool = True) -> dict:
        """Fetch a web page via Serper's scraper.

        Returns text + JSON-LD + metadata, optionally a markdown rendition.
        Préférable à un fetch brut : Serper gère le JS rendering et les
        anti-bot rudimentaires.

        Args:
            url: Page URL to scrape.
            include_markdown: Include a markdown version (default True, plus pratique pour LLM).
        """
        try:
            return _run("scrape_page", url=url, include_markdown=include_markdown)
        except RuntimeError as e:
            m = _SCRAPE_STATUS.search(str(e))
            if m and 500 <= int(m.group(1)) < 600:
                raise McpError(ErrorData(
                    code=INVALID_REQUEST,
                    message=(f"Scrape impossible pour cette URL ({url}) : la page a "
                             "bloqué le robot ou n'a pas pu être récupérée. Essaie une "
                             "autre source ou serper_web_search."),
                )) from None
            raise
