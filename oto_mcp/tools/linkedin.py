"""LinkedIn — scraping browser (délégué à o-browser-full).

oto-mcp ne lance plus de Chrome en propre : le scraping est **délégué au conteneur
o-browser-full** (cappé en mémoire, séparé du process auth). On ouvre une session
distante sur le **profil dédié du user** (`linkedin-<sub>`, créé par le pairing VNC)
et on pilote le Chrome distant via CDP. Un OOM du browser ne touche donc plus
`/api/me`. Réf : otomata-tech/oto-app#11.

**Profil d'abord, sans fallback cookie** : une session cookie côté serveur déconnecte
l'utilisateur (IP datacenter, session `li_at` partagée — issue oto-mcp#5). Sans profil
dédié → erreur actionnable pointant vers le pairing.

**Sérialisation (option A)** : un seul Chrome par conteneur → un verrou global
sérialise les scrapes concurrents.
"""
from __future__ import annotations

import asyncio
import os
import urllib.request
from contextlib import asynccontextmanager
from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import linkedin_pairing
from ..auth_hooks import current_user_sub_from_token


# Conteneur o-browser-full (même box, localhost par défaut). Override via env.
_OBROWSER_URL = os.environ.get("OBROWSER_URL", "http://127.0.0.1:8080").rstrip("/")

# Option A : un seul Chrome par conteneur → sérialiser les scrapes concurrents.
_BROWSER_LOCK = asyncio.Lock()

_ACCOUNT_URL = "https://app.oto.ninja/connections"


def _remote_profile(sub: str) -> str:
    """Nom du profil dédié du user dans le volume o-browser-full."""
    return f"linkedin-{sub}"


def _end_session() -> None:
    """Termine la session o-browser-full courante (best-effort).

    En mode cdp, `LinkedInClient.close()` ne ferme que la **page** ; la session (et
    le Chrome) du conteneur survit. Option A (un seul Chrome par conteneur) → on la
    termine après chaque scrape pour libérer le slot, sinon le prochain user (profil
    différent) tombe sur « Session already running ». À défaut, le watchdog du
    conteneur l'expire (~30 min).
    """
    try:
        req = urllib.request.Request(f"{_OBROWSER_URL}/api/sessions/current", method="DELETE")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _has_remote_profile(sub: str) -> bool:
    """Le user a-t-il un profil dédié ?

    Check **FS** (`linkedin_pairing.has_profile`) sur le volume partagé avec le
    conteneur — même source de vérité que `/api/me`, et il **suit les symlinks**
    (un sub peut pointer sur le profil d'un autre via symlink ; `GET /api/profiles`
    du conteneur, lui, filtre les symlinks). Le conteneur charge sans souci un
    user-data-dir symlinké, donc le check FS est le bon.
    """
    return linkedin_pairing.has_profile(sub)


def _no_profile_message() -> str:
    return (
        f"Aucune session LinkedIn configurée. Configure-la sur {_ACCOUNT_URL} "
        "(session navigateur dédiée — le pairing connecte un profil propre au "
        "serveur, indépendant de ta session, qui ne te déconnecte pas)."
    )


def _auth_wall_message(sub: str) -> str:
    """Remédiation quand la session du profil distant ne s'authentifie plus."""
    if _has_remote_profile(sub):
        return (
            "Profil navigateur LinkedIn déconnecté : sa session ne s'authentifie "
            f"plus (l'IP datacenter a pu être challengée). Re-paire-le sur {_ACCOUNT_URL} "
            "(LinkedIn → session navigateur → « reconfigurer »)."
        )
    return _no_profile_message()


def _identity_for(sub: str) -> str:
    """Bucket de rate-limit dédié par utilisateur Logto.

    Le LinkedInRateLimiter partage un fichier JSON — si on n'isole pas par sub,
    tous les users du MCP tapent dans le même compteur 10/h, 80/jour. On préfixe
    `mcp:` pour éviter toute collision avec les identités CLI locales.
    """
    return f"mcp:{sub}"


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
    from o_browser import RemoteBrowser
    from oto.tools.browser.linkedin import LinkedInClient

    @asynccontextmanager
    async def _session(sub: str, bypass_rate_limit: bool):
        """Session LinkedIn distante (profil dédié sur o-browser-full), sérialisée.

        Profil dédié EN PRIORITÉ : session propre côté serveur, ne déconnecte pas
        l'user. Pas de fallback cookie (déconnecte l'user — issue oto-mcp#5).
        Lève McpError si pas de profil dédié, ou si le conteneur est injoignable.
        """
        if not _has_remote_profile(sub):
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_no_profile_message()))
        async with _BROWSER_LOCK:  # option A : un Chrome à la fois sur le conteneur
            cdp_url = RemoteBrowser.ensure_session(_OBROWSER_URL, profile=_remote_profile(sub))
            if not cdp_url:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message="Service navigateur (o-browser-full) injoignable. Réessaie plus tard.",
                ))
            # cdp_url → LinkedInClient se connecte au Chrome distant (profil déjà loggé),
            # pas d'injection cookie (le profil porte la session).
            try:
                async with LinkedInClient(
                    cdp_url=cdp_url,
                    identity=_identity_for(sub),
                    rate_limit=not bypass_rate_limit,
                ) as li:
                    yield li
            finally:
                _end_session()  # libère le Chrome unique (option A)

    def _resolve_sub_and_client(self_bypass: bool):
        """Return (sub, session_factory). Profil dédié distant, sans fallback cookie."""
        sub = current_user_sub_from_token()
        if not sub:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="Unauthenticated — no user identity on the request.",
            ))
        return sub, lambda: _session(sub, self_bypass)

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

    @mcp.tool()
    async def linkedin_send_message(
        profile_url: str,
        message: str,
        dry_run: bool = False,
        bypass_rate_limit: bool = False,
    ) -> dict:
        """Send a direct message to a LinkedIn **1st-degree connection**.

        Action d'écriture (envoie réellement le message en ton nom). Le
        destinataire doit être une connexion 1er degré — sinon LinkedIn n'affiche
        pas de bouton « Message » et le tool lève une erreur (utiliser
        `linkedin_send_invitation` pour se connecter d'abord). Rate-limité par user.

        Args:
            profile_url: URL du profil destinataire (`linkedin.com/in/<slug>`).
            message: Corps du message.
            dry_run: Si True, tape le message mais ne clique PAS « envoyer »
                (screenshot côté serveur). Sert à valider la cible sans envoyer.
            bypass_rate_limit: """ + _BYPASS_DOC
        sub, make = _resolve_sub_and_client(bypass_rate_limit)
        try:
            async with make() as li:
                return await li.send_message(profile_url, message, dry_run=dry_run)
        except RuntimeError as e:
            raise _wrap_runtime(e, sub) from e

    @mcp.tool()
    async def linkedin_send_invitation(
        profile_url: str,
        note: Optional[str] = None,
        dry_run: bool = False,
        bypass_rate_limit: bool = False,
    ) -> dict:
        """Send a LinkedIn connection invitation (cold-outreach primitive).

        Action d'écriture (envoie réellement l'invitation en ton nom). Rate-limité.

        Args:
            profile_url: URL du profil cible (`linkedin.com/in/<slug>`).
            note: Note d'accompagnement optionnelle (≤300 caractères).
            dry_run: Si True, ouvre la modale mais n'envoie PAS (screenshot serveur).
            bypass_rate_limit: """ + _BYPASS_DOC
        sub, make = _resolve_sub_and_client(bypass_rate_limit)
        try:
            async with make() as li:
                return await li.send_invitation(profile_url, note=note, dry_run=dry_run)
        except RuntimeError as e:
            raise _wrap_runtime(e, sub) from e
