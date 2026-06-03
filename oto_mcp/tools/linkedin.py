"""LinkedIn — scraping browser.

**Mode pur cookie** : auth via le cookie `li_at` (+ user-agent capturé) stocké par
`sub` en table `users`. Le cookie est injecté dans un **vrai Google Chrome système**
(`_require_chrome_channel`) pour que l'empreinte TLS matche celle du Chrome de bureau
où l'utilisateur l'a capturé — sinon LinkedIn bloque (le Chromium bundlé de Patchright
a une empreinte différente, cassé depuis ~mai 2026).

Le provisioning de profil navigateur (VNC, `linkedin_pairing.py` + endpoints
`/api/settings/linkedin/browser/*`) existe toujours mais n'est **pas** consulté ici :
on isole d'abord le chemin cookie pour diagnostiquer ce qui bloque.

Si aucun cookie n'est configuré : McpError pointant vers app.oto.ninja/connections.
"""
from __future__ import annotations

import shutil
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import db
from ..auth_hooks import current_user_sub_from_token


def _require_chrome_channel() -> str:
    """Return the real Google Chrome channel, or raise.

    LinkedIn lie le cookie `li_at` (et la session du profil) à l'empreinte TLS du
    navigateur qui l'a créée — un Chrome de bureau. Le Chromium bundlé de Patchright
    a une empreinte différente → blocage. On force donc le **vrai** Chrome système,
    de la même famille que celui où l'utilisateur a capturé le cookie.

    Pas de fallback silencieux vers Chromium (ce qui re-casserait l'injection en
    douce) : si Chrome n'est pas installé, on lève une erreur actionnable.
    """
    for channel, binary in (("chrome", "google-chrome"), ("chrome-beta", "google-chrome-beta")):
        if shutil.which(binary):
            return channel
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(
            "Google Chrome système absent du serveur — requis pour que l'empreinte TLS "
            "matche le cookie/profil LinkedIn (le Chromium bundlé est bloqué par LinkedIn). "
            "Installer : apt-get install -y google-chrome-stable."
        ),
    ))


def _identity_for(sub: str) -> str:
    """Bucket de rate-limit dédié par utilisateur Logto.

    Le LinkedInRateLimiter partage un fichier JSON (`~/.cache/otomata/rate_limits.json`)
    — si on n'isole pas par sub, tous les users du MCP tapent dans le même
    compteur 10/h, 80/jour. On préfixe `mcp:` pour éviter toute collision
    avec les identités CLI locales.
    """
    return f"mcp:{sub}"


_ACCOUNT_URL = "https://app.oto.ninja/connections"


def _auth_wall_message(sub: str) -> str:
    """Actionable remediation for an auth-wall, en mode pur cookie."""
    if db.get_linkedin_session(sub):
        return (
            f"Cookie LinkedIn expiré ou invalide. Rafraîchis-le sur {_ACCOUNT_URL} "
            "ou via l'extension Oto Companion (capture le cookie depuis ton Chrome "
            "de bureau pour que l'empreinte TLS matche)."
        )
    return (
        f"Aucun cookie LinkedIn configuré. Ajoute-le sur {_ACCOUNT_URL} "
        "ou via l'extension Oto Companion."
    )


def _wrap_runtime(e: RuntimeError, sub: str) -> McpError:
    from oto.tools.browser.linkedin import LinkedInAuthWallError

    if isinstance(e, LinkedInAuthWallError):
        return McpError(ErrorData(code=INVALID_PARAMS, message=_auth_wall_message(sub)))
    msg = str(e)
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

    def _client(sub: str, bypass_rate_limit: bool, sess: Optional[dict] = None) -> "LinkedInClient":
        if not sess:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=_auth_wall_message(sub),
            ))
        return LinkedInClient(
            cookie=sess["cookie"],
            user_agent=sess["user_agent"],  # masque le leak HeadlessChrome
            channel=_require_chrome_channel(),  # vrai Chrome → empreinte TLS qui matche
            identity=_identity_for(sub),
            headless=True,
            rate_limit=not bypass_rate_limit,
        )

    def _resolve_sub_and_client(self_bypass: bool):
        """Return (sub, client_constructor). Profile-first, cookie fallback."""
        sub = current_user_sub_from_token()
        if not sub:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="Unauthenticated — no user identity on the request.",
            ))
        return sub, lambda: _client(sub, self_bypass, db.get_linkedin_session(sub))

    @mcp.tool()
    async def linkedin_scrape_profile(url: str, bypass_rate_limit: bool = False) -> dict:
        """Scrape a LinkedIn profile page (`linkedin.com/in/<slug>`).

        Returns identity, current/past positions, education, skills, summary.

        Rate-limited per user (10/h, 80/jour pour comptes free, fenêtre 8h–22h Paris).

        Args:
            url: LinkedIn profile URL.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub, make = _resolve_sub_and_client(bypass_rate_limit)
        try:
            async with make() as li:
                return await li.scrape_profile(url)
        except RuntimeError as e:
            raise _wrap_runtime(e, sub) from e

    @mcp.tool()
    async def linkedin_scrape_company(url: str, bypass_rate_limit: bool = False) -> dict:
        """Scrape a LinkedIn company page (`linkedin.com/company/<slug>`).

        Returns name, tagline, industry, employee count, HQ, specialties, about.

        Args:
            url: LinkedIn company URL.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub, make = _resolve_sub_and_client(bypass_rate_limit)
        try:
            async with make() as li:
                return await li.scrape_company(url)
        except RuntimeError as e:
            raise _wrap_runtime(e, sub) from e

    @mcp.tool()
    async def linkedin_search_companies(
        query: str, limit: int = 5, bypass_rate_limit: bool = False,
    ) -> list:
        """Search LinkedIn companies by free-text query.

        Args:
            query: Free-text query.
            limit: Max results.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub, make = _resolve_sub_and_client(bypass_rate_limit)
        try:
            async with make() as li:
                return await li.search_companies(query=query, limit=limit)
        except RuntimeError as e:
            raise _wrap_runtime(e, sub) from e

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
        sub, make = _resolve_sub_and_client(bypass_rate_limit)
        try:
            async with make() as li:
                return await li.search_people(
                    keywords=keywords, geo=geo, network=network, limit=limit, pages=pages,
                )
        except RuntimeError as e:
            raise _wrap_runtime(e, sub) from e

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
        sub, make = _resolve_sub_and_client(bypass_rate_limit)
        kw_list = keywords.split() if keywords else None
        try:
            async with make() as li:
                return await li.search_employees(
                    company_slug=company_slug, keywords=kw_list, limit=limit,
                )
        except RuntimeError as e:
            raise _wrap_runtime(e, sub) from e
