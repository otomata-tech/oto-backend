"""LinkedIn — scraping browser. Cookie `li_at` lu depuis la table `users` de
la DB (mis par l'utilisateur via `/settings`).

Si l'utilisateur n'a pas configuré son cookie : message d'erreur explicite
qui pointe vers `/settings`. Pas de fallback global pour éviter de mélanger
les sessions LinkedIn entre utilisateurs.
"""
from __future__ import annotations

from typing import Optional

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
    sess = db.get_linkedin_session(sub)
    if not sess:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "Aucun cookie LinkedIn configuré pour cet utilisateur. "
                "Va sur https://oto.ninja/account pour coller la valeur du cookie "
                "`li_at` de ton compte LinkedIn (l'user-agent est capturé automatiquement)."
            ),
        ))
    return sess


def register(mcp: FastMCP) -> None:
    from oto.tools.browser.linkedin import LinkedInClient

    @mcp.tool()
    async def linkedin_scrape_profile(url: str) -> dict:
        """Scrape a LinkedIn profile page (`linkedin.com/in/<slug>`).

        Returns identity, current/past positions, education, skills, summary.
        Le cookie de session de l'utilisateur courant est utilisé — il doit
        être configuré au préalable sur `/settings`.

        Rate-limited côté LinkedIn (10/h, 80/jour pour comptes free).
        """
        s = _get_session_or_raise()
        async with LinkedInClient(cookie=s["cookie"], user_agent=s["user_agent"], headless=True) as li:
            return await li.scrape_profile(url)

    @mcp.tool()
    async def linkedin_scrape_company(url: str) -> dict:
        """Scrape a LinkedIn company page (`linkedin.com/company/<slug>`).

        Returns name, tagline, industry, employee count, HQ, specialties, about.
        """
        s = _get_session_or_raise()
        async with LinkedInClient(cookie=s["cookie"], user_agent=s["user_agent"], headless=True) as li:
            return await li.scrape_company(url)

    @mcp.tool()
    async def linkedin_search_companies(query: str, limit: int = 5) -> list:
        """Search LinkedIn companies by free-text query."""
        s = _get_session_or_raise()
        async with LinkedInClient(cookie=s["cookie"], user_agent=s["user_agent"], headless=True) as li:
            return await li.search_companies(query=query, limit=limit)

    @mcp.tool()
    async def linkedin_search_people(
        keywords: str,
        geo: Optional[str] = None,
        network: Optional[str] = None,
        limit: int = 20,
        pages: int = 3,
    ) -> list:
        """Search LinkedIn people by free-text keywords.

        Args:
            keywords: Search terms (titles, names, skills…).
            geo: Optional LinkedIn geo URN to restrict results.
            network: Optional network filter ("F" = 1st degree, "S" = 2nd, "O" = out).
            limit: Max results returned.
            pages: Max search-result pages to walk (default 3, max ~10).
        """
        s = _get_session_or_raise()
        async with LinkedInClient(cookie=s["cookie"], user_agent=s["user_agent"], headless=True) as li:
            return await li.search_people(
                keywords=keywords, geo=geo, network=network, limit=limit, pages=pages,
            )

    @mcp.tool()
    async def linkedin_search_company_employees(
        company_slug: str,
        keywords: Optional[str] = None,
        limit: int = 10,
    ) -> list:
        """Find employees of a specific LinkedIn company.

        Args:
            company_slug: LinkedIn company slug (e.g. "anthropic" from
                `linkedin.com/company/anthropic`).
            keywords: Optional title/role keywords (e.g. "CEO OR CTO").
            limit: Max results.
        """
        s = _get_session_or_raise()
        kw_list = keywords.split() if keywords else None
        async with LinkedInClient(cookie=s["cookie"], user_agent=s["user_agent"], headless=True) as li:
            return await li.search_employees(
                company_slug=company_slug, keywords=kw_list, limit=limit,
            )
