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

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

_BASE = "https://api.browserbase.com/v1"
_BREVO_API = "https://workflow-apis.brevo.com/v1"
_BREVO_APP = "https://app.brevo.com/"


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


def session_status(session_id: str) -> str:
    return _req("GET", f"/sessions/{session_id}").get("status", "")


def release_session(session_id: str) -> None:
    try:
        _req("POST", f"/sessions/{session_id}",
             {"projectId": _project(), "status": "REQUEST_RELEASE"})
    except Exception:
        pass  # best-effort (la session expire seule sur timeout)


# --- exécution --------------------------------------------------------------
async def run_fetch(context_id: str, method: str, api_path: str,
                    body: Optional[dict] = None, *, base: str = _BREVO_API) -> dict:
    """Exécute UN `fetch(base+api_path)` depuis une page Brevo chargée dans une
    session éphémère du Context (la session vivante porte les cookies). Renvoie
    `{status, data}`. Lève BrowserbaseError si la session ne s'ouvre pas.

    Note coût : 1 session browser par appel. L'optimisation (réutiliser une
    session pour N appels d'un même run) est différée — corrige plus tard si le
    volume le justifie.
    """
    from patchright.async_api import async_playwright

    sess = start_session(context_id)
    sid = sess["id"]
    try:
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(sess["connectUrl"])
            ctx = b.contexts[0] if b.contexts else await b.new_context()
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await pg.goto(_BREVO_APP, wait_until="domcontentloaded", timeout=40000)
            result = await pg.evaluate(
                """async ({base, path, method, body}) => {
                    const r = await fetch(base + path, {
                        method, credentials: "include",
                        headers: {"content-type": "application/json"},
                        body: body ? JSON.stringify(body) : undefined,
                    });
                    let data;
                    try { data = await r.json(); }
                    catch (e) { data = {raw: (await r.text()).slice(0, 400)}; }
                    return {status: r.status, data};
                }""",
                {"base": base, "path": api_path, "method": method, "body": body})
            await b.close()
            return result
    finally:
        release_session(sid)
