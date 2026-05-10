"""Crunchbase — scraping browser. Cookies de session + UA stockés par
user via `/account` (similaire à LinkedIn, mais cookies = liste, pas
single string — Crunchbase utilise plusieurs cookies pour authentifier).

Si l'utilisateur n'a pas configuré sa session : message d'erreur
explicite qui pointe vers `/account`.
"""
from __future__ import annotations

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import db
from ..auth_hooks import current_user_sub_from_token


def _get_session_or_raise() -> dict:
    sub = current_user_sub_from_token()
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Unauthenticated — no user identity on the request.",
        ))
    sess = db.get_crunchbase_session(sub)
    if not sess:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "Aucune session Crunchbase configurée pour cet utilisateur. "
                "Va sur https://app.oto.ninja/account (section Crunchbase) "
                "pour coller tes cookies de session (export JSON depuis "
                "DevTools ou une extension Cookie Editor)."
            ),
        ))
    return sess


def register(mcp: FastMCP) -> None:
    from oto.tools.browser.crunchbase import CrunchbaseClient

    @mcp.tool()
    async def crunchbase_get_company(slug: str) -> dict:
        """Scrape a Crunchbase company page by slug.

        Args:
            slug: Crunchbase organization slug (e.g. "anthropic" from
                `crunchbase.com/organization/anthropic`).

        Returns name, description, founded, employees, location, funding
        summary, founders…
        """
        s = _get_session_or_raise()
        async with CrunchbaseClient(
            cookies=s["cookies"], user_agent=s["user_agent"], headless=True,
        ) as cb:
            return await cb.get_company(slug)

    @mcp.tool()
    async def crunchbase_get_person(slug: str) -> dict:
        """Scrape a Crunchbase person page by slug.

        Args:
            slug: Person slug from `crunchbase.com/person/<slug>`.
        """
        s = _get_session_or_raise()
        async with CrunchbaseClient(
            cookies=s["cookies"], user_agent=s["user_agent"], headless=True,
        ) as cb:
            return await cb.get_person(slug)

    @mcp.tool()
    async def crunchbase_search_companies(query: str, limit: int = 10) -> list:
        """Search companies by free-text query."""
        s = _get_session_or_raise()
        async with CrunchbaseClient(
            cookies=s["cookies"], user_agent=s["user_agent"], headless=True,
        ) as cb:
            return await cb.search_companies(query=query, limit=limit)

    @mcp.tool()
    async def crunchbase_search_people(query: str, limit: int = 10) -> list:
        """Search people by free-text query."""
        s = _get_session_or_raise()
        async with CrunchbaseClient(
            cookies=s["cookies"], user_agent=s["user_agent"], headless=True,
        ) as cb:
            return await cb.search_people(query=query, limit=limit)

    @mcp.tool()
    async def crunchbase_get_funding_rounds(slug: str) -> list:
        """List funding rounds for a company.

        Args:
            slug: Crunchbase organization slug.

        Returns a list of rounds with date, type, amount, investors.
        """
        s = _get_session_or_raise()
        async with CrunchbaseClient(
            cookies=s["cookies"], user_agent=s["user_agent"], headless=True,
        ) as cb:
            return await cb.get_funding_rounds(slug)
