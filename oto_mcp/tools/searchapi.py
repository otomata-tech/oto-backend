"""SearchApi — recherche multi-moteurs via SearchApi.io (scope complet de l'API).

Wrappe l'API REST **SearchApi.io** (`GET https://www.searchapi.io/api/v1/search`,
un seul endpoint paramétré par `engine`). **Surface consolidée (ADR 0047
§Amendement appliqué à un connecteur)** : l'API n'ayant qu'UN endpoint, elle
n'expose qu'UN tool — `searchapi_search`, la verticale choisie par `engine`.
Les 6 tools typés d'avant (`searchapi_{web,news,jobs,scholar,maps,youtube}_search`)
ne différaient du générique que par un `engine` codé en dur et par des champs
nommés (`q`/`gl`/`hl`/`location`/`num`/`page`, communs à la plupart des moteurs) :
ils sont devenus des valeurs d'`engine` + des paramètres typés du tool générique.
`engine` reste **ouvert** (n'importe quel id SearchApi, y compris un moteur que ce
module ne connaît pas) — c'est la capacité même du connecteur, on ne la ferme pas
par une allowlist.

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
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, url_perimeter
from ..connectors import verify as connector_verify

_BASE_URL = "https://www.searchapi.io/api/v1/search"
_ACCOUNT_URL = "https://www.searchapi.io/api/v1/me"
_TIMEOUT = 30.0


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /api/v1/me` — endpoint dédié « account usage » (crédits restants,
    limite horaire), documenté « without requiring a specific plan level » :
    gratuit, contrairement à `/api/v1/search` (facturé à la requête, ce que
    ce module wrappe). Bearer header (jamais en query — `_run` de ce module
    applique déjà cette règle sur `/search`, cf. #284).

    **Authentifié ≠ utilisable** (classe oto#69) : ne lit pas le solde (pas de
    forme de champ confirmée dans le temps imparti) — `auth` seul.
    """
    import requests

    r = requests.get(_ACCOUNT_URL, headers={"Authorization": f"Bearer {fields['key']}"},
                     timeout=15)
    r.raise_for_status()


def register(mcp: FastMCP) -> None:
    connector_verify.register("searchapi", _verify)


    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

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
        if not engine or not engine.strip():
            raise _bad(
                "searchapi_search requiert `engine` (la verticale SearchApi), "
                "ex. 'google', 'google_news', 'google_jobs', 'google_scholar', "
                "'google_maps', 'youtube' — voir la liste complète dans la "
                "description du tool. Tout id de moteur SearchApi est accepté."
            )
        if query is None and not params:
            raise _bad(
                f"searchapi_search(engine='{engine}') requiert `query` (le `q` du "
                "moteur) — ou `params` pour un moteur dont l'entrée n'est pas `q` "
                "(ex. engine='google_lens' avec params={'url': …})."
            )
        payload: dict = {"q": query, "gl": country, "hl": language,
                         "location": location, "num": num, "page": page}
        if params:
            payload.update(params)
        return url_perimeter.filter_results(_run(engine, payload),
                                            url_perimeter.perimeter_of_call())
