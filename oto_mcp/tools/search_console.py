"""Google Search Console — via a SERVICE ACCOUNT KEY: search performance, URL
inspection, sitemaps.

Wraps `oto.tools.search_console.SearchConsoleClient`. Credential with ONE secret
field (`secret_kind="fields"`), as `google_analytics`.

**A pair**: `search_console` reads, `search_console_act` submits a sitemap, the only
write the API has. Neither can ask Google to index a page: the API offers no such
thing, and the tool description says so rather than leaving an agent to look for it.

**Projection**: performance comes back as a TABLE (`columns` + `rows`), an inspection
reduced to what says whether the URL is indexed. `full=True` returns the raw answer.

**One verb, one URL or many**: `url` or `urls`, exactly one. A batch stops on a
refusal that would repeat for every URL (key, access, quota) and keeps what it read.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

_CONNECTOR = "search_console"
_FIELD = "service_account_json"
_OPS = ("sites", "performance", "inspect", "sitemaps")
# `asyncio.wait_for(..., timeout=45)` on the REST face, about a second per inspection.
_MAX_INSPECT = 20
_WINDOW_DAYS = 28
# Google's last two days are incomplete.
_LAG_DAYS = 2


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    """The upstream refusal, translated into guidance. Read from the refusal's CLASS,
    never from the text."""
    from oto.tools.search_console import (SearchConsoleDenied, SearchConsoleInvalid,
                                          SearchConsoleServiceDisabled,
                                          ServiceAccountAuthError)
    if isinstance(e, SearchConsoleDenied):
        quoi = e.site or "this property"
        return (f"The service account {e.client_email} does not have access to {quoi}. "
                "Add this email as a user of the property in Search Console (Settings → "
                "Users and permissions; Full is needed to add a sitemap). "
                "`search_console()` lists what it already sees.")
    if isinstance(e, SearchConsoleServiceDisabled):
        return ("The Google Search Console API is not enabled in the service account's "
                "Google Cloud project: enable it (Google Cloud console → APIs & "
                f"Services), then retry. Google detail: {e.message}")
    if isinstance(e, SearchConsoleInvalid):
        return (f"Search Console refused the request: {e.message.strip()} — check the "
                "property (exact form from `search_console()`), the dates and the "
                "dimensions; an inspected URL must belong to the property.")
    if isinstance(e, ServiceAccountAuthError):
        return (f"Google refuses to issue a token for this service account key "
                f"({e.body}): key deleted or revoked, or service account disabled "
                "— an administrator must upload a new JSON key.")
    status = e.status_code
    if status == 429:
        return ("Search Console: quota reached (429) — retry later. URL inspection allows "
                "2,000 URLs a day per property.")
    if status >= 500:
        return f"Search Console is temporarily unavailable (HTTP {status}) — retry later."
    return f"Search Console refused the request (HTTP {status}): {getattr(e, 'message', '') or e.body}"


def _readable(sites: list) -> list:
    """`siteUnverifiedUser` is a property Google lists and refuses to read."""
    return [s for s in sites if s.get("permissionLevel") != "siteUnverifiedUser"]


def _verify(fields: dict, config: dict | None = None) -> dict:  # noqa: ARG001
    """"Test the connection" probe: the list of sites, with no side effect.

    ⚠️ Zero readable property is a REFUSAL, not a green: the key is valid but reads
    nothing — a service account forgotten in Search Console."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.search_console import SearchConsoleClient

    try:
        client = SearchConsoleClient(fields[_FIELD])
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e)) from None
    try:
        sites = _readable(client.list_sites())
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(_upstream_message(e)) from None
        raise
    if not sites:
        raise connector_verify.NonAutorise(
            f"The key authenticates ({client.client_email}) but sees no property: add "
            "this email as a user of the property in Search Console (Settings → Users "
            "and permissions).")
    return {"identity": {"service_account": client.client_email, "sites": len(sites)}}


def _default_range(today: Optional[date] = None) -> tuple[str, str]:
    end = (today or date.today()) - timedelta(days=_LAG_DAYS)
    return (end - timedelta(days=_WINDOW_DAYS - 1)).isoformat(), end.isoformat()


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.search_console import (DEFAULT_ROW_LIMIT, SearchConsoleClient,
                                          flatten_inspection, flatten_performance)

    connector_verify.register(_CONNECTOR, _verify)

    def _client() -> SearchConsoleClient:
        return SearchConsoleClient(access.resolve_credential_fields(_CONNECTOR)[_FIELD])

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    def _needs(op: str, **given) -> None:
        for name, value in given.items():
            if not value:
                raise _bad(f"op='{op}' needs `{name}`. Nothing was done.")

    def _inspect_batch(client, site: str, urls: list[str], language: Optional[str],
                       full: bool) -> dict:
        if len(urls) > _MAX_INSPECT:
            raise _bad(f"{len(urls)} URLs: at most {_MAX_INSPECT} per call, the rest "
                       "in another call. Nothing was done.")
        results, failed = [], []
        for i, url in enumerate(urls):
            try:
                resp = client.inspect_url(site, url, language_code=language)
            except UpstreamHTTPError as e:
                failed.append({"url": url, "error": _upstream_message(e)})
                if e.status_code in (401, 403, 429):
                    failed.extend({"url": u, "error": "not read: stopped on the refusal above"}
                                  for u in urls[i + 1:])
                    break
                continue
            results.append({"url": url, **(resp if full else flatten_inspection(resp))})
        return {"total": len(urls), "inspected": results, "failed": failed}

    @mcp.tool(name="search_console", annotations=LECTURE)
    def search_console(
        op: Literal[_OPS] = "sites",
        site: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        dimensions: Optional[list[str]] = None,
        filters: Optional[dict | list[dict]] = None,
        search_type: Optional[str] = None,
        row_limit: int = DEFAULT_ROW_LIMIT,
        start_row: int = 0,
        url: Optional[str] = None,
        urls: Optional[list[str]] = None,
        language_code: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """What Google Search knows about your sites: clicks, positions, whether a
        URL is indexed. Read-only.

        `op`:
        - `sites` (default) — the properties this connector can read, call it first:
          every other op needs a `site` from here, in the exact form returned
          (`sc-domain:example.com` or `https://example.com/`).
        - `performance` (`site`) — clicks, impressions, ctr and average position,
          broken down by `dimensions`: `query`, `page`, `country`, `device`, `date`,
          `searchAppearance`. Default window: the 28 days ending 2 days ago, as the
          last days are incomplete. Returns `columns` + `rows`, `next_row` when
          more remain.
        - `inspect` (`site`, `url` or `urls`) — what Google knows about a URL:
          `verdict` (PASS = on Google), `coverage` (the reason, in Search Console's
          words), last crawl, canonical. At most 20 URLs per call; a batch returns
          `{total, inspected, failed}`. Quota: 2,000 URLs a day per property.
        - `sitemaps` (`site`) — submitted sitemaps, last read, errors.

        ⚠️ Nothing here asks Google to index a page: the API has no such call.
        Submitting a sitemap is `search_console_act`.

        Args:
            op: what to read (see above).
            site: the property, from op=sites.
            start_date: performance — YYYY-MM-DD.
            end_date: performance — YYYY-MM-DD.
            dimensions: performance — e.g. ["query"], ["page", "date"].
            filters: performance — `{"page": "https://…", "country": "fra"}` for exact
                matches, or Google's filters as a list, e.g. [{"dimension": "page",
                "operator": "contains", "expression": "/articles/"}].
            search_type: performance — web (default), image, video, news, discover,
                googleNews.
            row_limit: performance — rows returned, 25000 at most.
            start_row: performance — rows to skip, for paging.
            url: inspect — one URL of the property.
            urls: inspect — several, instead of `url`.
            language_code: inspect — language of `coverage`, e.g. "en-US".
            full: performance / inspect — Google's raw answer.
        """
        if op == "sites":
            return _run(lambda: {"sites": _readable(_client().list_sites())})
        _needs(op, site=site)
        if op == "sitemaps":
            return _run(lambda: {"sitemaps": _client().list_sitemaps(site)})
        if op == "inspect":
            if bool(url) == bool(urls):
                raise _bad("op='inspect' takes exactly one of `url` or `urls`. Nothing was done.")
            if urls:
                return _run(lambda: _inspect_batch(_client(), site, urls, language_code, full))
            resp = _run(lambda: _client().inspect_url(site, url, language_code=language_code))
            return resp if full else {"url": url, **flatten_inspection(resp)}

        start, end = _default_range()
        window = (start_date or start, end_date or end)
        resp = _run(lambda: _client().search_analytics(
            site, start_date=window[0], end_date=window[1], dimensions=dimensions,
            search_type=search_type, filters=filters, row_limit=row_limit,
            start_row=start_row))
        if full:
            return resp
        out = {"site": site, "date_range": f"{window[0]} → {window[1]}",
               **flatten_performance(resp, dimensions)}
        if len(out["rows"]) == row_limit:
            out["next_row"] = start_row + row_limit
        return out

    @mcp.tool(name="search_console_act", annotations=ACTE)
    def search_console_act(
        op: Literal["add_sitemap"],
        site: str,
        sitemap: str,
    ) -> dict:
        """Submit, or resubmit, a sitemap to Google for a property. Tells Google where
        the sitemap is: it does not index anything by itself.

        Needs the service account to be an Owner or Full user of the property;
        Restricted can only read.

        Args:
            op: `add_sitemap`.
            site: the property, from `search_console()`.
            sitemap: the sitemap's full URL.
        """
        _run(lambda: _client().add_sitemap(site, sitemap))
        return {"site": site, "sitemap": sitemap, "submitted": True}
