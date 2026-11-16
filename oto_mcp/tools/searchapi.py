"""SearchApi — multi-engine search via SearchApi.io (full scope of the API).

MCP envelope of `oto.tools.searchapi.SearchApiClient` (oto-core). The API has ONE
endpoint, so the connector exposes ONE tool — `searchapi_search`, the vertical picked
by `engine` (ADR 0047 §Amendment applied to a connector). `engine` stays **open** (any
SearchApi id): that is the connector's capability, not closed by an allowlist. Payload
construction, the Bearer header (never the key in the query) and the local refusals
live in the lib.

Here, the host's share only: the key resolved per call via
`access.resolve_api_key("searchapi")` (user key, the org's shared credential, else
platform key + daily quota for members — same regime as serper/serpapi), platform
usage counted (`note_call_trace`), the URL perimeter applied to the results, a lib
`ValueError` turned into `INVALID_PARAMS`. An upstream refusal (`UpstreamHTTPError`)
is left to the error envelope.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS
from oto.tools.searchapi import SearchApiClient

from .. import access, session_org, url_perimeter
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe — otomata-tech/oto#69. Covers `auth` ONLY.

    `GET /api/v1/me` (`SearchApiClient.account`): free account usage, unlike
    `/search` (billed per request). **Authenticated ≠ usable** (class oto#69): the
    balance is not read — `auth` only.
    """
    SearchApiClient(fields["key"]).account()


def _client() -> tuple[SearchApiClient, bool]:
    key, is_platform = access.resolve_api_key("searchapi")
    return SearchApiClient(key), is_platform


def register(mcp: FastMCP) -> None:
    connector_verify.register("searchapi", _verify)

    @mcp.tool()
    def searchapi_search(
        engine: str,
        query: Optional[str] = None,
        country: Optional[str] = None,
        language: Optional[str] = None,
        location: Optional[str] = None,
        num: Optional[int] = None,
        page: Optional[int] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """SearchApi.io call — ONE endpoint, the vertical picked by `engine`.

        Reaches ANY SearchApi engine: pass the engine id plus either the typed
        fields below (the params common to most engines) or `params` for
        anything engine-specific. Returns the raw SearchApi JSON payload. Under
        a project with `excluded_url_prefixes`, matching results are dropped and
        counted.

        Engines — common ids (any SearchApi engine id is accepted, this list is
        not a closed set):
            Google verticals: google, google_news, google_maps, google_jobs,
            google_scholar, google_images, google_videos, google_shopping,
            google_trends, google_lens, google_autocomplete, google_finance,
            google_play, google_events, google_flights, google_hotels.
            Other engines: youtube, youtube_transcripts, bing, bing_news,
            baidu, duckduckgo, yahoo, yandex, amazon_search, ebay_search,
            walmart_search, apple_app_store.
        See searchapi.io docs for each engine's parameters.

        Flagship verticals — what each one is and what it returns:
        - engine="google": Google web search. Returns 'organic_results'.
          Takes query, country, language, location, num, page.
        - engine="google_news": Google News search — recent news for a query.
          Returns 'organic_results' (news articles with source, date, link).
          Takes query, country, language.
        - engine="google_jobs": Google Jobs search — live job postings
          (job-board sourcing). Returns 'jobs' (each with title, company,
          location, apply options). Takes query, location, country, language.
        - engine="google_scholar": Google Scholar search — academic papers /
          citations for a query. Returns 'organic_results' (title, authors,
          publication, citations). Takes query, language.
        - engine="google_maps": Google Maps search — local places/businesses
          for a query. Returns 'local_results' (name, address, phone, rating,
          coordinates). Takes query, location, language.
        - engine="youtube": YouTube search — videos, channels, playlists for a
          query. Returns 'videos' / 'channels' / 'playlists'. Takes query,
          country, language.

        Any other engine: use `params` for its own inputs, e.g.
        engine="google_lens" + params={"url": "https://…"}, or
        engine="google_flights" + params={"departure_id": "CDG", …}.
        The typed fields are the COMMON case, not a universal set: an engine
        that does not support one of them rejects the call upstream (4xx).

        Either `query` or `params` must be provided.

        Args:
            engine: SearchApi engine id (see the list above). Required — no
                default vertical is assumed.
            query: search query (SearchApi `q`).
            country: 2-letter country code (`gl`, e.g. "fr", "us").
            language: UI language (`hl`, e.g. "fr", "en").
            location: geographic location / anchor, e.g. "Paris, France".
            num: number of results per page.
            page: result page (1-based).
            params: engine-specific params, e.g. {"q": "pizza", "gl": "us",
                "hl": "en", "location": "Paris, France"}. Merged LAST: a key
                given here overrides the typed field it duplicates. Use it for
                engines whose input is not `q`, and for any filter without a
                typed field (time range, sorting, ids…).
        """
        client, is_platform = _client()
        try:
            data = client.search(engine, query=query, country=country, language=language,
                                 location=location, num=num, page=page, params=params)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e
        if is_platform:
            session_org.note_call_trace(units=1)
        return url_perimeter.filter_results(data, url_perimeter.perimeter_of_call())
