"""Browserbase — substrat d'exécution navigateur HÉBERGÉ (ADR à venir).

Pour les connecteurs d'**API privée cookie-bound** (ex. `brevo` automation), dont
le token n'est accepté que depuis une **session navigateur vivante** (cf. en-tête
`tools/brevo.py`). On ne lance PAS de browser sur la box (OOM, et la session ne se
transplante pas par export de cookie) : on **loue** un Chrome distant chez Browserbase.

Modèle (prouvé 2026-06-24) :
- **Context** Browserbase = profil persistant per-user qui détient la session loguée.
  L'utilisateur se logue UNE fois (Live View interactive → il gère SSO/captcha/2FA),
  les cookies se persistent dans le Context.
- **Exécution** = on ouvre une session éphémère sur le Context (pas de browser 24/7),
  on s'y branche en CDP, et on exécute `fetch(...)` DANS la page → le `fetch` porte la
  session vivante. Spin-up à la demande = coût ≈ minutes de browser par usage réel.

Creds **plateforme** (infra, pas per-user) en env : `BROWSERBASE_API_KEY` +
`BROWSERBASE_PROJECT_ID`. Le credential per-user = le **Context ID** (coffre).
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

_BASE = "https://api.browserbase.com/v1"


class BrowserbaseError(RuntimeError):
    pass


def _key() -> str:
    k = os.environ.get("BROWSERBASE_API_KEY")
    if not k:
        raise BrowserbaseError("BROWSERBASE_API_KEY absent de l'environnement.")
    return k


def _project() -> str:
    p = os.environ.get("BROWSERBASE_PROJECT_ID")
    if not p:
        raise BrowserbaseError("BROWSERBASE_PROJECT_ID absent de l'environnement.")
    return p


def is_configured() -> bool:
    return bool(os.environ.get("BROWSERBASE_API_KEY") and os.environ.get("BROWSERBASE_PROJECT_ID"))


def _req(method: str, path: str, body: Optional[dict] = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(_BASE + path, data=data, method=method,
                               headers={"X-BB-API-Key": _key(),
                                        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise BrowserbaseError(f"Browserbase {method} {path} → {e.code}: "
                               f"{e.read()[:200].decode(errors='replace')}")


# --- contexts ---------------------------------------------------------------
def create_context() -> str:
    """Crée un Context (profil persistant) et renvoie son id."""
    return _req("POST", "/contexts", {"projectId": _project()})["id"]


# --- sessions ---------------------------------------------------------------
def start_session(context_id: str, *, keep_alive: bool = False,
                  timeout: int = 600) -> dict:
    """Ouvre une session sur un Context (persist=true). Renvoie le JSON
    Browserbase ({id, connectUrl, ...})."""
    body = {"projectId": _project(),
            "browserSettings": {"context": {"id": context_id, "persist": True}},
            "timeout": timeout}
    if keep_alive:
        body["keepAlive"] = True
    return _req("POST", "/sessions", body)


def start_ephemeral_session(*, timeout: int = 120) -> dict:
    """Session JETABLE, SANS Context (#348, cran ③ du lecteur) : un vrai Chrome
    anonyme, aucun profil, aucun cookie du coffre — rien à persister, rien à
    fuiter. C'est LA différence avec le connecteur `browser` (sites à login) :
    ici on lit une page PUBLIQUE qui se défend, le navigateur n'a pas
    d'identité. Timeout court : une lecture, pas une session de travail."""
    return _req("POST", "/sessions",
                {"projectId": _project(), "timeout": timeout})


def connect_url(session_id: str) -> str:
    """URL CDP (wss) pour se (re)brancher à une session existante — la clé API y
    est injectée côté serveur, jamais exposée à l'agent."""
    return f"wss://connect.browserbase.com?apiKey={_key()}&sessionId={session_id}"


def live_view_url(session_id: str) -> str:
    """URL Live View interactive (l'utilisateur prend la main pour se loguer)."""
    dbg = _req("GET", f"/sessions/{session_id}/debug")
    url = dbg.get("debuggerFullscreenUrl") or dbg.get("debuggerUrl")
    if not url:
        raise BrowserbaseError("Live View URL introuvable dans /debug.")
    return url


async def navigate(session_id: str, url: str) -> None:
    """Amène la page d'une session **keep-alive** existante sur `url`, puis se détache
    (la session distante reste vivante, la page reste sur `url`). Sert à ouvrir la Live
    View directement sur la page de login du connecteur (sinon `about:blank`, l'user ne
    sait pas où aller). Best-effort : une nav échouée n'empêche pas d'afficher la Live
    View (l'user peut taper l'URL à la main)."""
    from patchright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(connect_url(session_id))
        try:
            ctx = b.contexts[0] if b.contexts else await b.new_context()
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await pg.goto(url, wait_until="domcontentloaded", timeout=40000)
        finally:
            await b.close()


def session_status(session_id: str) -> str:
    return _req("GET", f"/sessions/{session_id}").get("status", "")


def release_session(session_id: str) -> None:
    try:
        _req("POST", f"/sessions/{session_id}",
             {"projectId": _project(), "status": "REQUEST_RELEASE"})
    # noqa: SILENT — best-effort : la session hébergée expire seule sur timeout
    except Exception:
        pass  # best-effort (la session expire seule sur timeout)


# --- exécution --------------------------------------------------------------
async def run_page_eval(context_id: str, app: str, page_function: str,
                        arg: Any = None) -> Any:
    """Charge la page `app` dans une session éphémère du Context et y exécute
    `page_function` (source JS d'une fonction async `(arg) => …`) avec `arg`,
    renvoyant sa valeur. Primitive bas-niveau du substrat : ouvre/ferme la session,
    gère le CDP — le connecteur fournit le JS (un `fetch` same-origin, une sonde…).

    `app` = page à charger pour porter l'origine/les cookies de session (propre au
    connecteur). Tout `fetch` du JS doit rester **same-origin** avec `app` pour que
    `credentials: "include"` porte les cookies. Lève BrowserbaseError si la session
    ne s'ouvre pas.

    Note coût : 1 session browser par appel. L'optimisation (réutiliser une session
    pour N appels d'un même run) est différée — corrige plus tard si le volume le justifie.
    """
    from patchright.async_api import async_playwright

    # Ouverture et libération de session sont des appels HTTP SYNCHRONES (urllib,
    # 30 s de délai d'attente) au milieu d'une fonction `async def` : appelés
    # nûment, ils figent tout le processus le temps que Browserbase réponde
    # (oto-backend#867). Les protéger ici couvre d'un coup les quatre familles
    # d'outils qui passent par ce substrat — c'est le segment, pas ses appelants,
    # qui porte le défaut.
    sess = await asyncio.to_thread(start_session, context_id)
    sid = sess["id"]
    try:
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(sess["connectUrl"])
            try:
                ctx = b.contexts[0] if b.contexts else await b.new_context()
                pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await pg.goto(app, wait_until="domcontentloaded", timeout=40000)
                return await pg.evaluate(page_function, arg)
            finally:
                await b.close()
    finally:
        await asyncio.to_thread(release_session, sid)


async def fetch_page(context_id: str, url: str, *, as_html: bool = False,
                     wait_until: str = "domcontentloaded",
                     timeout: int = 40000) -> dict:
    """Charge `url` dans une session éphémère du Context et renvoie le **contenu
    complet** de la page rendue : `{status, final_url, title, content}`.

    C'est le pendant « page » de `run_fetch` (qui, lui, tape une API JSON et
    tronque son repli texte à 400 caractères — inutilisable pour lire un article).
    `as_html=False` (défaut) renvoie le texte rendu (`innerText` du body : ce que
    l'humain lit, sans balises) ; `as_html=True` le DOM sérialisé après rendu.

    `status` = code HTTP de la navigation principale (None si Chrome n'a pas
    exposé de réponse, ex. redirection interne SPA). La session vivante porte les
    cookies du Context → une page derrière login rend son contenu logué.

    Note coût : 1 session browser par appel (cf. `run_page_eval`).
    """
    from patchright.async_api import async_playwright

    sess = await asyncio.to_thread(start_session, context_id)
    sid = sess["id"]
    try:
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(sess["connectUrl"])
            try:
                ctx = b.contexts[0] if b.contexts else await b.new_context()
                pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
                resp = await pg.goto(url, wait_until=wait_until, timeout=timeout)
                content = (await pg.content() if as_html
                           else await pg.evaluate("() => document.body ? document.body.innerText : ''"))
                return {"status": resp.status if resp is not None else None,
                        "final_url": pg.url,
                        "title": await pg.title(),
                        "content": content or ""}
            finally:
                await b.close()
    finally:
        await asyncio.to_thread(release_session, sid)


async def fetch_page_ephemeral(url: str, *, as_html: bool = False,
                               wait_until: str = "domcontentloaded",
                               timeout: int = 40000) -> dict:
    """`fetch_page`, mais sur une session JETABLE sans Context (#348, cran ③) :
    un Chrome anonyme lit une page PUBLIQUE qui se défend (challenge anti-robot,
    rendu JS, lenteur), sans profil ni cookie. Même contrat de sortie
    ({status, final_url, title, content})."""
    from patchright.async_api import async_playwright

    sess = await asyncio.to_thread(start_ephemeral_session)
    sid = sess["id"]
    try:
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(sess["connectUrl"])
            try:
                ctx = b.contexts[0] if b.contexts else await b.new_context()
                pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
                resp = await pg.goto(url, wait_until=wait_until, timeout=timeout)
                content = (await pg.content() if as_html
                           else await pg.evaluate("() => document.body ? document.body.innerText : ''"))
                return {"status": resp.status if resp is not None else None,
                        "final_url": pg.url,
                        "title": await pg.title(),
                        "content": content or ""}
            finally:
                await b.close()
    finally:
        await asyncio.to_thread(release_session, sid)


async def host_cookies(session_id: str, url: str) -> int:
    """Nombre de cookies que la session VIVANTE porte pour `url`. Sonde générique
    « cette session a-t-elle un état sur ce site ? » — le seul signal de login
    qu'on puisse lire sans rien savoir du site (cf. `browser_session` §verify
    générique). 0 = presque toujours « pas logué » ; >0 ne prouve pas le login
    (un cookie anonyme suffit à faire 1) — d'où l'échappatoire `force` côté tool."""
    from patchright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(connect_url(session_id))
        try:
            ctx = b.contexts[0] if b.contexts else await b.new_context()
            return len(await ctx.cookies(url))
        finally:
            await b.close()


# JS partagé : un `fetch(base+path)` same-origin avec headers JSON. `run_fetch`
# l'instancie pour les connecteurs simples (brevo) ; les connecteurs à headers
# spécifiques (CSRF tournant…) écrivent leur propre JS et passent par `run_page_eval`.
_FETCH_JS = """async ({base, path, method, body}) => {
    const r = await fetch(base + path, {
        method, credentials: "include",
        headers: {"content-type": "application/json"},
        body: body ? JSON.stringify(body) : undefined,
    });
    // Le corps d'une Response ne se lit qu'UNE fois : `r.json()` VERROUILLE le
    // flux avant même d'échouer, donc un `catch` qui rappelle `r.text()` lève
    // « body stream already read » et masque la réponse RÉELLE. Vécu en prod le
    // 27/08 sur un DELETE de dossier GED : la suppression était partie, le tool
    // a répondu « Erreur interne du serveur. » (signal #600, tool_calls#1039702).
    // On lit le texte une seule fois, puis on le parse.
    const txt = await r.text();
    let data;
    try { data = txt ? JSON.parse(txt) : null; }
    catch (e) { data = {raw: txt.slice(0, 400)}; }
    return {status: r.status, data};
}"""


async def run_fetch(context_id: str, method: str, api_path: str,
                    body: Optional[dict] = None, *, base: str, app: str) -> dict:
    """Exécute UN `fetch(base+api_path)` depuis une page chargée dans une session
    éphémère du Context (la session vivante porte les cookies). Renvoie
    `{status, data}`. Lève BrowserbaseError si la session ne s'ouvre pas.

    `base` = racine de l'API privée (ex. `https://workflow-apis.brevo.com/v1`),
    `app`  = page à charger pour porter l'origine/les cookies (ex.
    `https://app.brevo.com/`). Les DEUX sont propres au connecteur — le substrat
    n'en hardcode aucun (un connecteur = un couple base/app).

    ⚠️ Le `fetch` doit être **same-origin** avec `base` pour porter les cookies de
    session : charger une `app` du MÊME host que `base` (sinon `credentials:
    "include"` est cross-origin et la session ne suit pas).
    """
    return await run_page_eval(
        context_id, app, _FETCH_JS,
        {"base": base, "path": api_path, "method": method, "body": body})
