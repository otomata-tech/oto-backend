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
        user = db.get_user(sub) or {}
        email = user.get("email") or "<no row>"
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Aucun cookie LinkedIn configuré pour cet utilisateur (sub={sub}, email={email}). "
                "Va sur https://oto.ninja/account pour coller la valeur du cookie "
                "`li_at` de ton compte LinkedIn (l'user-agent est capturé automatiquement)."
            ),
        ))
    return sess


def _identity_for(sub: str) -> str:
    """Bucket de rate-limit dédié par utilisateur Logto.

    Le LinkedInRateLimiter partage un fichier JSON (`~/.cache/otomata/rate_limits.json`)
    — si on n'isole pas par sub, tous les users du MCP tapent dans le même
    compteur 10/h, 80/jour. On préfixe `mcp:` pour éviter toute collision
    avec les identités CLI locales.
    """
    return f"mcp:{sub}"


def _wrap_runtime(e: RuntimeError) -> McpError:
    msg = str(e)
    if "session expired" in msg.lower() or "li_at" in msg:
        return McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "Cookie LinkedIn expiré. L'utilisateur doit le rafraîchir sur "
                "https://oto.ninja/account (ou via l'extension Oto Companion)."
            ),
        ))
    if "Outside active hours" in msg or "Rate limit" in msg:
        return McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"LinkedIn rate-limit actif : {msg}. Réessaie plus tard.",
        ))
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


_BYPASS_DOC = (
    "If True, ignore the per-user LinkedIn rate-limit (10/h, 80/day profile "
    "visits, active hours 8h–22h Paris). Use sparingly — bypass = higher ban risk. "
    "Default False."
)


def register(mcp: FastMCP) -> None:
    from oto.tools.browser.linkedin import LinkedInClient

    def _client(s: dict, sub: str, bypass_rate_limit: bool) -> "LinkedInClient":
        return LinkedInClient(
            cookie=s["cookie"],
            user_agent=s["user_agent"],
            identity=_identity_for(sub),
            headless=True,
            rate_limit=not bypass_rate_limit,
        )

    @mcp.tool()
    async def linkedin_scrape_profile(url: str, bypass_rate_limit: bool = False) -> dict:
        """Scrape a LinkedIn profile page (`linkedin.com/in/<slug>`).

        Returns identity, current/past positions, education, skills, summary.
        Le cookie de session de l'utilisateur courant est utilisé — il doit
        être configuré au préalable sur `/settings`.

        Rate-limited per user (10/h, 80/jour pour comptes free, fenêtre 8h–22h Paris).

        Args:
            url: LinkedIn profile URL.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub = current_user_sub_from_token()
        s = _get_session_or_raise()
        try:
            async with _client(s, sub, bypass_rate_limit) as li:
                return await li.scrape_profile(url)
        except RuntimeError as e:
            raise _wrap_runtime(e) from e

    @mcp.tool()
    async def linkedin_scrape_company(url: str, bypass_rate_limit: bool = False) -> dict:
        """Scrape a LinkedIn company page (`linkedin.com/company/<slug>`).

        Returns name, tagline, industry, employee count, HQ, specialties, about.

        Args:
            url: LinkedIn company URL.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub = current_user_sub_from_token()
        s = _get_session_or_raise()
        try:
            async with _client(s, sub, bypass_rate_limit) as li:
                return await li.scrape_company(url)
        except RuntimeError as e:
            raise _wrap_runtime(e) from e

    @mcp.tool()
    async def linkedin_search_companies(
        query: str, limit: int = 5, bypass_rate_limit: bool = False,
    ) -> list:
        """Search LinkedIn companies by free-text query.

        Args:
            query: Free-text query.
            limit: Max results.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub = current_user_sub_from_token()
        s = _get_session_or_raise()
        try:
            async with _client(s, sub, bypass_rate_limit) as li:
                return await li.search_companies(query=query, limit=limit)
        except RuntimeError as e:
            raise _wrap_runtime(e) from e

    @mcp.tool()
    async def linkedin_search_people(
        keywords: str,
        geo: Optional[str] = None,
        network: Optional[str] = None,
        limit: int = 20,
        pages: int = 3,
        bypass_rate_limit: bool = False,
    ) -> list:
        """Search LinkedIn people by free-text keywords.

        Args:
            keywords: Search terms (titles, names, skills…).
            geo: Optional LinkedIn geo URN to restrict results.
            network: Optional network filter ("F" = 1st degree, "S" = 2nd, "O" = out).
            limit: Max results returned.
            pages: Max search-result pages to walk (default 3, max ~10).
            bypass_rate_limit: """ + _BYPASS_DOC
        sub = current_user_sub_from_token()
        s = _get_session_or_raise()
        try:
            async with _client(s, sub, bypass_rate_limit) as li:
                return await li.search_people(
                    keywords=keywords, geo=geo, network=network, limit=limit, pages=pages,
                )
        except RuntimeError as e:
            raise _wrap_runtime(e) from e

    @mcp.tool()
    async def linkedin_search_company_employees(
        company_slug: str,
        keywords: Optional[str] = None,
        limit: int = 10,
        bypass_rate_limit: bool = False,
    ) -> list:
        """Find employees of a specific LinkedIn company.

        Args:
            company_slug: LinkedIn company slug (e.g. "anthropic" from
                `linkedin.com/company/anthropic`).
            keywords: Optional title/role keywords (e.g. "CEO OR CTO").
            limit: Max results.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub = current_user_sub_from_token()
        s = _get_session_or_raise()
        kw_list = keywords.split() if keywords else None
        try:
            async with _client(s, sub, bypass_rate_limit) as li:
                return await li.search_employees(
                    company_slug=company_slug, keywords=kw_list, limit=limit,
                )
        except RuntimeError as e:
            raise _wrap_runtime(e) from e
