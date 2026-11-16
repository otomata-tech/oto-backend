"""Google Search Console — the person's own properties, by Google sign-in, multi-account.

Ninth service of the Google account (2026-11-15): scope `webmasters.readonly`, granted
from its card in one click. Reads see the Search Console properties THEIR Google user
can open, nothing more — no key to paste, no service account to add to a property.

**One tool, `search_console`, read-only** (`<ns>` of the `<ns>`/`<ns>_act` pair; no
`_act`: the scope asked cannot write). Its ops and arguments are those of the
service-account connector `google_search_console` (described in oto-core), and so are
the answers (Google's JSON; `list_sitemaps` under `items`), so a recipe written for one
runs on the other by changing the tool name:
- `list_sites` — the properties, with the permission level of the user on each;
- `query_search_analytics` — clicks, impressions, CTR, position, grouped and filtered;
- `inspect_url` — the index status of one URL (2,000 a day per property);
- `list_sitemaps` / `get_sitemap` — the submitted sitemaps.

The call goes straight to `searchconsole.googleapis.com` with the account's OAuth token
(`requests`, like `google_ads`): no oto-core client, the API is five plain routes.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Literal, Optional, get_args
from urllib.parse import quote

import requests
from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from .. import access
from ..auth import google as google_oauth
from ..mcp_errors import McpError
from .lecture import LECTURE

_SERVICE = "search_console"
_BASE = "https://searchconsole.googleapis.com"
# Each Google call: under the 45 s of a REST invocation, token refresh included.
_HTTP_TIMEOUT = (10, 25)
_TOKEN_TIMEOUT_S = 15
_MAX_ROW_LIMIT = 25_000

_Op = Literal["list_sites", "query_search_analytics", "inspect_url",
              "list_sitemaps", "get_sitemap"]
_OPS = get_args(_Op)
_Dimension = Literal["query", "page", "country", "device", "date", "searchAppearance", "hour"]
_DIMENSIONS = get_args(_Dimension)
_SearchType = Literal["web", "image", "video", "news", "discover", "googleNews"]
_Aggregation = Literal["auto", "byPage", "byProperty", "byNewsShowcasePanel"]
_DataState = Literal["final", "all", "hourly_all"]

_SITE = re.compile(r"^(sc-domain:[^\s/]+|https?://\S+/)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL = re.compile(r"^https?://\S+$")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Required argument for THIS op: the empty string counts as absent."""
    if value is None or value == "" or value == []:
        raise _bad(f"op='{op}' requires {name}")
    return value


def _site(site_url: Optional[str], op: str) -> str:
    site_url = _need(site_url, "site_url", op)
    if not _SITE.match(site_url):
        raise _bad("site_url must be the property as op='list_sites' names it: "
                   "sc-domain:example.com for a Domain property, or a full URL WITH its "
                   "trailing slash (https://www.example.com/) for a URL-prefix property.")
    return site_url


def _path_site(site_url: str) -> str:
    # The whole property in ONE path segment: ":" and "/" percent-encoded, as Google's
    # own reference sends sc-domain%3Aexample.com.
    return quote(site_url, safe="")


# --- the token, per call -------------------------------------------------------

def _token_for_user(account: Optional[str] = None) -> str:
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service=_SERVICE)
    except RuntimeError as e:
        raise _bad(str(e))
    return creds.token


async def _token(account: Optional[str] = None) -> str:
    """Off the event loop (vault read + possible refresh), with its own deadline."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_token_for_user, account),
                                      timeout=_TOKEN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google did not respond within {_TOKEN_TIMEOUT_S}s "
                   "(token refresh) — try again.")


# --- one HTTP call ---------------------------------------------------------------

def _reason(body: Any) -> tuple[str, str]:
    """(reason, message) of a Google error body, empty when it has none."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return "", ""
    message = str(err.get("message") or "")
    for key in ("errors", "details"):
        for item in err.get(key) or []:
            if isinstance(item, dict) and item.get("reason"):
                return str(item["reason"]), message
    return str(err.get("status") or ""), message


def _api_error(status: int, body: Any) -> McpError:
    """A Google refusal → what to do next, read from the status and the reason."""
    reason, message = _reason(body)
    said = f" Google said: {message}" if message else ""
    if status == 400:
        return _bad("Search Console rejected the request as invalid: check the dates "
                    "(YYYY-MM-DD), the dimensions, filters and aggregation, and the "
                    "property name." + said)
    if status == 401:
        return _bad("Google rejected this account's token: reconnect it from the "
                    "« Search Console (Google sign-in) » card, then retry.")
    if status == 403 and reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        return _bad("The Google Search Console API is not enabled in the Google Cloud "
                    "project of this platform's sign-in app: the platform's administrator "
                    "enables it (APIs & Services > Library). Reconnecting changes nothing.")
    if status == 403 and reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT") \
            and "sufficient permission for site" not in message:
        return _bad("This Google account has not granted Search Console read access: "
                    "authorize it from the « Search Console (Google sign-in) » card.")
    if status in (403, 429) and (reason in ("quotaExceeded", "dailyLimitExceeded")
                                 or "per day" in message):
        return _bad("A daily Search Console quota is spent (URL inspection allows 2,000 a "
                    "day per property): retry tomorrow.")
    if status == 403:
        return _bad("Your Google user cannot read this property: pick one that "
                    "op='list_sites' returns, or sign in with the Google account that has "
                    "access to it (pass its address as `account`)." + said)
    if status == 404:
        return _bad("Not found in Search Console: check the property and the sitemap "
                    "address (op='list_sitemaps')." + said)
    if status == 429:
        return _bad("Search Console is throttling these requests (per property: 1,200 "
                    "performance queries and 600 URL inspections a minute): retry after a "
                    "delay.")
    return _bad(f"Search Console answered HTTP {status}: retry later." + said)


def _call(token: str, method: str, path: str, *, params: Optional[dict] = None,
          body: Optional[dict] = None) -> Any:
    try:
        resp = requests.request(method, _BASE + path, params=params, json=body,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=_HTTP_TIMEOUT, allow_redirects=False)
    except requests.Timeout:
        raise _bad(f"Search Console did not answer within {_HTTP_TIMEOUT[1]}s — narrow "
                   "the request (shorter range, fewer rows) and try again.")
    except requests.RequestException as e:
        raise _bad(f"Search Console is unreachable ({type(e).__name__}) — try again later.")
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    if resp.status_code >= 300:
        raise _api_error(resp.status_code, data)
    return data


# --- the ops ------------------------------------------------------------------------

def _analytics_body(op: str, start_date, end_date, dimensions, type, dimension_filter_groups,
                    aggregation_type, data_state, row_limit, start_row) -> dict:
    for name, value in (("start_date", start_date), ("end_date", end_date)):
        _need(value, name, op)
        if not _DATE.match(value):
            raise _bad(f"{name} must be a date YYYY-MM-DD.")
    if end_date < start_date:
        raise _bad("end_date is before start_date.")
    if dimensions:
        unknown = [d for d in dimensions if d not in _DIMENSIONS]
        if unknown:
            raise _bad(f"unknown dimension(s) {unknown}: use {list(_DIMENSIONS)}.")
        if len(set(dimensions)) != len(dimensions):
            raise _bad("a dimension is listed twice.")
        if "hour" in dimensions and data_state != "hourly_all":
            raise _bad("the hour dimension needs data_state='hourly_all'.")
    if not 1 <= row_limit <= _MAX_ROW_LIMIT:
        raise _bad(f"row_limit goes from 1 to {_MAX_ROW_LIMIT:,}.")
    if start_row < 0:
        raise _bad("start_row starts at 0.")
    body: dict = {"startDate": start_date, "endDate": end_date, "type": type,
                  "aggregationType": aggregation_type, "dataState": data_state,
                  "rowLimit": row_limit, "startRow": start_row}
    if dimensions:
        body["dimensions"] = list(dimensions)
    if dimension_filter_groups:
        body["dimensionFilterGroups"] = dimension_filter_groups
    return body


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=LECTURE)
    async def search_console(
        op: _Op = "list_sites",
        site_url: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        dimensions: Optional[list[_Dimension]] = None,
        type: _SearchType = "web",
        dimension_filter_groups: Optional[list[dict]] = None,
        aggregation_type: _Aggregation = "auto",
        data_state: _DataState = "final",
        row_limit: int = 1000,
        start_row: int = 0,
        inspection_url: Optional[str] = None,
        language_code: str = "en-US",
        sitemap_index: Optional[str] = None,
        feedpath: Optional[str] = None,
        account: Optional[str] = None,
    ) -> dict:
        """Read Google Search Console for the properties YOUR Google user can open (read-only).

        Take it with the person's own Google connection; with the org's service account, take `google_search_console`.

        Ops (all free, bounded by Google's quotas):
        - `list_sites` (default) → {siteEntry: [{siteUrl, permissionLevel}]}. `siteUrl`
          is the `site_url` every other op takes: `sc-domain:example.com` (Domain
          property) or `https://www.example.com/` WITH its trailing slash (URL prefix).
        - `query_search_analytics` (site_url, start_date, end_date; optional
          dimensions, type, dimension_filter_groups, aggregation_type, data_state,
          row_limit, start_row) → rows of {keys, clicks, impressions, ctr, position},
          keys in the order of `dimensions`. No dimension = one total row. Dates
          inclusive, Pacific time; data lags ~2 days (`data_state="all"` adds fresh,
          incomplete days). At most `row_limit` rows (≤ 25,000): for more, call again
          with start_row = previous start_row + row_limit until fewer rows come back.
          A filter: {"filters": [{"dimension": "page", "operator": "contains",
          "expression": "/blog/"}]}; operators equals, notEquals, contains,
          notContains, includingRegex, excludingRegex; country is ISO alpha-3 (FRA),
          device MOBILE / DESKTOP / TABLET.
        - `inspect_url` (site_url, inspection_url; optional language_code) → how
          Google last saw that URL: verdict, coverageState, lastCrawlTime, canonicals…
          Bounded to 2,000 a day per property: inspect the pages that matter.
        - `list_sitemaps` (site_url; optional sitemap_index) and `get_sitemap`
          (site_url, feedpath = the sitemap's full URL) → submitted sitemaps, with
          warnings, errors and submitted counts (strings).

        Args:
            account: email of the Google account to use — same choice as `_account`;
                `google_accounts` tells which ones authorized Search Console.
        """
        if op not in _OPS:
            raise _bad(f"op must be one of {list(_OPS)}")
        if op == "list_sites":
            token = await _token(account)
            return await asyncio.to_thread(_call, token, "GET", "/webmasters/v3/sites")
        site = _site(site_url, op)
        if op == "query_search_analytics":
            body = _analytics_body(op, start_date, end_date, dimensions, type,
                                   dimension_filter_groups, aggregation_type, data_state,
                                   row_limit, start_row)
            token = await _token(account)
            return await asyncio.to_thread(
                _call, token, "POST",
                f"/webmasters/v3/sites/{_path_site(site)}/searchAnalytics/query", body=body)
        if op == "inspect_url":
            url = _need(inspection_url, "inspection_url", op)
            if not _URL.match(url):
                raise _bad("inspection_url must be a full http(s) URL under site_url.")
            token = await _token(account)
            return await asyncio.to_thread(
                _call, token, "POST", "/v1/urlInspection/index:inspect",
                body={"siteUrl": site, "inspectionUrl": url, "languageCode": language_code})
        if op == "list_sitemaps":
            params = {"sitemapIndex": sitemap_index} if sitemap_index else None
            token = await _token(account)
            data = await asyncio.to_thread(
                _call, token, "GET", f"/webmasters/v3/sites/{_path_site(site)}/sitemaps",
                params=params)
            # `items`, as the described connector's `output: {items: sitemap}` serves it.
            return {"items": data.get("sitemap") or []}
        # get_sitemap
        path = _need(feedpath, "feedpath", op)
        if not _URL.match(path):
            raise _bad("feedpath must be the sitemap's full http(s) URL "
                       "(as op='list_sitemaps' gives it in `path`).")
        token = await _token(account)
        return await asyncio.to_thread(
            _call, token, "GET",
            f"/webmasters/v3/sites/{_path_site(site)}/sitemaps/{quote(path, safe='')}")
