"""Crunchbase — fiches société / personne via l'API PRIVÉE du frontend.

⚠️ API privée (celle de l'UI web `www.crunchbase.com/v4/data/*`), auth = **session
navigateur vivante** (cookies de login). Elle reflète le schéma documenté de l'API
publique v4 (`api.crunchbase.com/v4/data/*` : mêmes `field_ids`/`card_ids`,
endpoints `entities/organizations|people/{permalink}`, `searches/*`,
`autocompletes`) mais sans `user_key` — la session loguée tient lieu d'auth. Peut
casser sans préavis côté Crunchbase.

Exécution — **Browserbase** (`oto_mcp/browserbase.py`), même substrat que `brevo` :
le token n'est accepté que depuis une **session navigateur vivante** (un `httpx`
brut est rejeté, une session ne se transplante pas par export de cookie, et un
browser in-process sur la box = OOM + dépendance à un Chrome local). On loue donc un
Chrome distant : l'utilisateur se logue UNE fois via la **Live View**
(`crunchbase_connect_start`, il gère SSO/captcha/2FA), sa session persiste dans un
**Context** Browserbase (= le credential per-user, coffre `crunchbase`), et chaque
appel `/v4/data` s'exécute en `fetch()` DANS une session éphémère du Context
(`browserbase.run_fetch`, same-origin `www.crunchbase.com`). Creds plateforme = env
`BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID`.

Remplace l'ancien scraping DOM in-process (o-browser `CrunchbaseClient`), cassé
silencieusement sur la box sans binaire navigateur (cf. ADR 0026).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote, urlencode

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR

from .. import access, browserbase, db
from ..auth_hooks import current_user_sub_from_token

# Couple (API privée, page d'origine) propre à Crunchbase. Le `fetch` est
# same-origin avec l'app (www.crunchbase.com) → il porte les cookies de session ;
# l'API `/v4/data` vit sous le MÊME host (pas un sous-domaine séparé comme brevo).
_API = "https://www.crunchbase.com/v4/data"
_APP = "https://www.crunchbase.com/"


def _err(msg: str, code: int = INVALID_PARAMS) -> McpError:
    return McpError(ErrorData(code=code, message=msg))


def _sub() -> str:
    sub = None
    try:
        sub = current_user_sub_from_token()
    except Exception:
        pass
    if not sub:
        raise _err("Auth requise — ce tool ne marche que sur le transport HTTP authentifié.")
    return sub


def _context_id() -> str:
    """Context Browserbase de l'utilisateur (= sa session Crunchbase loguée), résolu
    du coffre. Lève une McpError actionnable si Crunchbase n'est pas connecté."""
    try:
        return access.resolve_credential("crunchbase", want="byo").key
    except McpError:
        raise _err("Crunchbase non connecté. Lance `crunchbase_connect_start` pour te "
                   "loguer (une fois) via la Live View.")


def _permalink(value: str, kind: str) -> str:
    """Extrait le permalink (slug) d'une valeur qui peut être une URL complète.
    `kind` = 'organization' | 'person'."""
    v = (value or "").strip()
    marker = f"/{kind}/"
    if marker in v:
        v = v.split(marker, 1)[1]
    return v.split("/")[0].split("?")[0]


async def _api(method: str, path: str, body: Optional[dict] = None) -> dict:
    """Exécute un appel `/v4/data` dans la session Browserbase de l'user. Renvoie le
    `data` décodé. Lève une McpError actionnable sinon."""
    if not browserbase.is_configured():
        raise _err("Browserbase non configuré côté plateforme "
                   "(BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID).", code=INTERNAL_ERROR)
    ctx_id = _context_id()
    try:
        res = await browserbase.run_fetch(ctx_id, method, path, body, base=_API, app=_APP)
    except browserbase.BrowserbaseError as e:
        raise _err(f"Exécution Browserbase échouée : {e}", code=INTERNAL_ERROR)
    st = res.get("status")
    if st in (401, 403):
        raise _err("Session Crunchbase expirée / déconnectée — relance `crunchbase_connect_start`.")
    if not (200 <= (st or 0) < 300):
        raise _err(f"Crunchbase a renvoyé {st} : {str(res.get('data'))[:200]}", code=INTERNAL_ERROR)
    return res["data"]


def register(mcp: FastMCP) -> None:

    # --- Onboarding (Live View) --------------------------------------------
    @mcp.tool()
    def crunchbase_connect_start(ctx: Context) -> dict:
        """Démarre la connexion à Crunchbase. Ouvre un navigateur distant et renvoie
        une **`live_view_url`** : ouvre-la, connecte-toi à Crunchbase normalement
        (email/mot de passe, SSO, captcha — tu gères tout dans cette fenêtre). Puis
        appelle `crunchbase_connect_status(context_id, session_id)` avec les valeurs
        renvoyées pour finaliser (ta session est mémorisée ; à refaire seulement
        quand elle expire).
        """
        _sub()
        if not browserbase.is_configured():
            raise _err("Browserbase non configuré côté plateforme.", code=INTERNAL_ERROR)
        try:
            context_id = browserbase.create_context()
            sess = browserbase.start_session(context_id, keep_alive=True, timeout=900)
            live = browserbase.live_view_url(sess["id"])
        except browserbase.BrowserbaseError as e:
            raise _err(f"Browserbase : {e}", code=INTERNAL_ERROR)
        return {
            "live_view_url": live,
            "context_id": context_id,
            "session_id": sess["id"],
            "instructions": "Ouvre `live_view_url`, connecte-toi à Crunchbase, puis "
                            "appelle `crunchbase_connect_status` avec context_id + session_id.",
        }

    @mcp.tool()
    async def crunchbase_connect_status(ctx: Context, context_id: str,
                                        session_id: str) -> dict:
        """Finalise la connexion Crunchbase. Vérifie que tu t'es bien logué dans la
        Live View (en appelant l'API privée depuis ta session) ; si oui,
        **mémorise** ta session (le Context) pour les prochains appels. Renvoie
        `{connected}`. Rappelle-le si `connected=false` (pas encore logué)."""
        sub = _sub()
        from patchright.async_api import async_playwright
        authed = False
        try:
            async with async_playwright() as p:
                b = await p.chromium.connect_over_cdp(browserbase.connect_url(session_id))
                c = b.contexts[0] if b.contexts else await b.new_context()
                pg = c.pages[0] if c.pages else await c.new_page()
                await pg.goto(_APP, wait_until="domcontentloaded", timeout=40000)
                # Sanity same-origin : l'API privée ne répond 200 que loguée.
                res = await pg.evaluate(
                    """async (base) => {
                        try {
                            const r = await fetch(base + "/entities/organizations/crunchbase"
                                + "?field_ids=identifier", {credentials: "include",
                                headers: {"content-type": "application/json"}});
                            return r.status;
                        } catch (e) { return 0; }
                    }""", _API)
                authed = (res == 200)
                await b.close()
        except Exception as e:
            raise _err(f"Impossible de vérifier la session ({e}).", code=INTERNAL_ERROR)
        if not authed:
            return {"connected": False,
                    "hint": "Pas encore logué — connecte-toi dans la Live View puis relance."}
        browserbase.release_session(session_id)  # persiste le Context
        db.set_user_api_key(sub, "crunchbase", context_id)
        return {"connected": True, "context_id": context_id}

    # --- Lecture ------------------------------------------------------------
    @mcp.tool()
    async def crunchbase_get_company(slug: str) -> dict:
        """Fiche société Crunchbase par permalink (slug).

        Args:
            slug: permalink de l'organisation (ex. "anthropic" depuis
                `crunchbase.com/organization/anthropic`) — une URL complète est aussi
                acceptée (le slug en est extrait).

        Renvoie la réponse brute `/v4/data` : `properties` (firmographie : nom,
        description, fondation, localisation, effectif, financement…) + `cards`
        (`founders`, `raised_funding_rounds`). Données structurées, à exploiter telles
        quelles.
        """
        permalink = _permalink(slug, "organization")
        qs = urlencode({"card_ids": "founders,raised_funding_rounds"})
        return await _api("GET", f"/entities/organizations/{quote(permalink)}?{qs}")

    @mcp.tool()
    async def crunchbase_get_person(slug: str) -> dict:
        """Fiche personne Crunchbase par permalink (slug).

        Args:
            slug: permalink depuis `crunchbase.com/person/<slug>` (URL complète
                acceptée).

        Renvoie la réponse brute `/v4/data` : `properties` de la personne (nom, bio,
        liens sociaux…).
        """
        permalink = _permalink(slug, "person")
        return await _api("GET", f"/entities/people/{quote(permalink)}")

    @mcp.tool()
    async def crunchbase_search_companies(query: str, limit: int = 10) -> dict:
        """Recherche d'organisations par texte libre (autocomplete Crunchbase).

        Renvoie `{entities}` (bruts) : chaque entrée porte un `identifier` avec
        `permalink` (→ slug pour `crunchbase_get_company`), `value` (nom) et
        `entity_def_id`.
        """
        qs = urlencode({"query": query, "collection_ids": "organization.companies",
                        "limit": max(1, min(int(limit), 25))})
        return await _api("GET", f"/autocompletes?{qs}")

    @mcp.tool()
    async def crunchbase_search_people(query: str, limit: int = 10) -> dict:
        """Recherche de personnes par texte libre (autocomplete Crunchbase).

        Renvoie `{entities}` (bruts) : chaque entrée porte un `identifier` avec
        `permalink` (→ slug pour `crunchbase_get_person`), `value` (nom) et
        `entity_def_id`.
        """
        qs = urlencode({"query": query, "collection_ids": "person.people",
                        "limit": max(1, min(int(limit), 25))})
        return await _api("GET", f"/autocompletes?{qs}")

    @mcp.tool()
    async def crunchbase_get_funding_rounds(slug: str) -> dict:
        """Tours de financement d'une organisation.

        Args:
            slug: permalink de l'organisation (URL complète acceptée).

        Renvoie la carte `raised_funding_rounds` brute (date, type, montant,
        investisseurs) depuis `/v4/data`.
        """
        permalink = _permalink(slug, "organization")
        qs = urlencode({"card_ids": "raised_funding_rounds"})
        return await _api("GET", f"/entities/organizations/{quote(permalink)}?{qs}")
