"""Pennylane GED (DMS) — bac documentaire via l'API PRIVÉE de la SPA.

⚠️ La GED de Pennylane **n'est pas exposée par l'API publique** (le connecteur
keyé `pennylane` ne peut donc pas y écrire — son token ne porte aucun scope DMS).
Elle l'est par l'**API interne** de `app.pennylane.com` (cookie de session + CSRF
tournant), sous le scope société `/companies/{cid}/dms/…`. C'est un connecteur
**distinct** de `pennylane` : credential de nature différente (session navigateur,
pas une clé API).

Exécution — **Browserbase** (`oto_mcp/browserbase.py`), même substrat que
`crunchbase`/`brevo` : l'API interne n'accepte les appels que depuis une **session
navigateur vivante** (un `httpx` brut risque le blocage Cloudflare, et une session
ne se transplante pas par export de cookie). L'utilisateur se logue UNE fois via la
**Live View** (`pennylaneged_connect_start`), sa session persiste dans un **Context**
Browserbase (= le credential per-user, coffre `pennylaneged`), et chaque appel DMS
s'exécute en `fetch()` DANS une session éphémère du Context, same-origin
`app.pennylane.com`. Creds plateforme = env `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID`.

**Exigences de l'API interne** (gérées par le JS in-page `_FETCH_JS`) : header
`accept: application/json` (sinon 404 HTML — contrainte Rails), `x-requested-with:
XMLHttpRequest`, et sur les écritures `x-csrf-token` = valeur **tournante** du cookie
`my_csrf_token` (relue à CHAQUE appel — le `<meta csrf-token>` est périmé dès le 1er XHR).

**Split data-plane (RGPD)** — l'upload d'un fichier NE fait PAS transiter les octets
par Oto (cf. ADR / issue #31). `pennylaneged_request_upload` (control plane) demande
une **URL S3 présignée** ; l'agent LOCAL fait le `PUT` des octets **directement** sur
S3 (jamais par Oto, jamais via MCP) ; puis `pennylaneged_finalize` (control plane)
crée l'entrée DMS depuis le `signed_id`. Les octets vont `local → S3 Pennylane`, leur
destination de toute façon.

Statut : flux RE **validé manuellement** (18/06, compte test Fidens) ; **reste à
smoker en live** sur le substrat Browserbase (CSRF in-page + longévité de session).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR

from .. import access, browser_session, browserbase
from ..auth_hooks import current_user_sub_from_token

# Origine de la SPA — toutes les routes internes (DMS, direct_uploads, crm) en
# dérivent. La page chargée pour porter la session est same-origin (un chemin de
# cette origine), donc `fetch("/companies/…")` porte les cookies.
_ORIGIN = "https://app.pennylane.com"

# JS in-page propre à Pennylane : lit le CSRF tournant du cookie `my_csrf_token` à
# l'instant de l'appel et pose les headers Rails attendus. `path` est un chemin
# absolu de l'origine `app.pennylane.com` (le `fetch` est donc same-origin).
_FETCH_JS = """async ({path, method, body}) => {
    const m = document.cookie.match(/(?:^|;\\s*)my_csrf_token=([^;]+)/);
    const headers = {"accept": "application/json", "x-requested-with": "XMLHttpRequest"};
    if (m) headers["x-csrf-token"] = decodeURIComponent(m[1]);
    if (body) headers["content-type"] = "application/json";
    const r = await fetch(path, {
        method, credentials: "include", headers,
        body: body ? JSON.stringify(body) : undefined,
    });
    let data;
    try { data = await r.json(); }
    catch (e) { data = {raw: (await r.text()).slice(0, 400)}; }
    return {status: r.status, data};
}"""


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
    """Context Browserbase de l'utilisateur (= sa session Pennylane loguée), résolu du
    coffre. Lève une McpError actionnable si la GED n'est pas connectée."""
    try:
        return access.resolve_credential("pennylaneged", want="byo").key
    except McpError:
        raise _err("Pennylane GED non connecté. Lance `pennylaneged_connect_start` pour "
                   "te loguer (une fois) à Pennylane via la Live View.")


def _company_app(company_id: int) -> str:
    """Page à charger pour amorcer le contexte société (la SPA exige une navigation
    sur la vue DMS de la société avant que `/companies/{cid}/context` réponde 200)."""
    return f"{_ORIGIN}/companies/{int(company_id)}/dms/items"


async def _call(app: str, path: str, method: str = "GET",
                body: Optional[dict] = None) -> dict:
    """Exécute un appel d'API interne depuis la session Browserbase de l'user. Renvoie
    le `{status, data}` brut décodé. Lève une McpError actionnable sinon."""
    if not browserbase.is_configured():
        raise _err("Browserbase non configuré côté plateforme "
                   "(BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID).", code=INTERNAL_ERROR)
    ctx_id = _context_id()
    try:
        res = await browserbase.run_page_eval(
            ctx_id, app, _FETCH_JS, {"path": path, "method": method, "body": body})
    except browserbase.BrowserbaseError as e:
        raise _err(f"Exécution Browserbase échouée : {e}", code=INTERNAL_ERROR)
    st = res.get("status")
    if st in (401, 403):
        raise _err("Session Pennylane expirée / déconnectée — relance `pennylaneged_connect_start`.")
    if not (200 <= (st or 0) < 300):
        raise _err(f"Pennylane GED a renvoyé {st} : {str(res.get('data'))[:200]}",
                   code=INTERNAL_ERROR)
    return res.get("data") or {}


async def _verify_session(session_id: str) -> bool:
    """Login Pennylane confirmé ? Sonde une route interne authentifiée DEPUIS la session
    vivante (same-origin) : elle ne répond 200 que loguée. Partagé par les deux surfaces
    de connexion (dashboard REST + MCP) via `browser_session`."""
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(browserbase.connect_url(session_id))
        try:
            c = b.contexts[0] if b.contexts else await b.new_context()
            pg = c.pages[0] if c.pages else await c.new_page()
            await pg.goto(f"{_ORIGIN}/", wait_until="domcontentloaded", timeout=40000)
            res = await pg.evaluate(
                """async () => {
                    try {
                        const r = await fetch("/crm/flow_companies?page=1", {
                            credentials: "include",
                            headers: {"accept": "application/json",
                                      "x-requested-with": "XMLHttpRequest"}});
                        return r.status;
                    } catch (e) { return 0; }
                }""")
            return res == 200
        finally:
            await b.close()


# Déclare Pennylane GED comme connecteur à session navigateur (start générique + ce
# verify) — alimente le flux de connexion REST (dashboard) ET MCP. À l'import.
browser_session.register("pennylaneged", _verify_session, login_url=f"{_ORIGIN}/")


def register(mcp: FastMCP) -> None:

    # --- Onboarding (Live View) --------------------------------------------
    @mcp.tool()
    def pennylaneged_connect_start(ctx: Context) -> dict:
        """Démarre la connexion à la GED Pennylane. Ouvre un navigateur distant et
        renvoie une **`live_view_url`** : ouvre-la, connecte-toi à Pennylane normalement
        (email/mot de passe, SSO, 2FA — tu gères tout dans cette fenêtre). Puis appelle
        `pennylaneged_connect_status(context_id, session_id)` avec les valeurs renvoyées
        pour finaliser (ta session est mémorisée ; à refaire seulement quand elle expire).
        """
        sub = _sub()
        try:
            out = browser_session.start(sub, "pennylaneged")
        except browser_session.SessionError as e:
            raise _err(str(e), code=INTERNAL_ERROR)
        out["instructions"] = ("Ouvre `live_view_url`, connecte-toi à Pennylane, puis "
                               "appelle `pennylaneged_connect_status` avec context_id + session_id.")
        return out

    @mcp.tool()
    async def pennylaneged_connect_status(ctx: Context, context_id: str,
                                          session_id: str) -> dict:
        """Finalise la connexion à la GED Pennylane. Vérifie que tu t'es bien logué dans
        la Live View (en appelant l'API interne depuis ta session) ; si oui, **mémorise**
        ta session (le Context) pour les prochains appels. Renvoie `{connected}`.
        Rappelle-le si `connected=false` (pas encore logué)."""
        sub = _sub()
        try:
            connected = await browser_session.finalize(sub, "pennylaneged", context_id, session_id)
        except browser_session.SessionError as e:
            raise _err(str(e), code=INTERNAL_ERROR)
        if not connected:
            return {"connected": False,
                    "hint": "Pas encore logué — connecte-toi dans la Live View puis relance."}
        return {"connected": True, "context_id": context_id}

    # --- Résolution « où » (control plane) ----------------------------------
    @mcp.tool()
    async def pennylaneged_companies(page: int = 1) -> dict:
        """Liste les sociétés accessibles (côté cabinet) pour résoudre le `company_id`
        cible d'une opération GED.

        Tape la route interne `/crm/flow_companies` (paginée). Renvoie la réponse brute :
        chaque société porte son `id` (= `company_id` à passer aux autres tools), son
        `name` et son `client_code`. Le SIREN n'est pas dans cette liste — pour
        désambiguïser un homonyme, croiser avec `/companies/{id}/context` (`reg_no`).

        Args:
            page: page de pagination (1-based).
        """
        qs = urlencode({"page": max(1, int(page))})
        return await _call(f"{_ORIGIN}/", f"/crm/flow_companies?{qs}")

    # --- Arborescence / dossiers --------------------------------------------
    @mcp.tool()
    async def pennylaneged_tree(company_id: int, item_type: str = "DmsFolder") -> dict:
        """Lit l'arborescence GED d'une société.

        Args:
            company_id: id de la société (cf. `pennylaneged_companies`).
            item_type: type d'items listés — `DmsFolder` (dossiers, défaut) ou `DmsFile`.

        Renvoie la liste brute des items `[{id, name, itemable_type, parent_id,
        folders_count, …}]` — utilise les `id`/`parent_id` pour cibler un `parent_id`
        de création ou un item à supprimer.
        """
        qs = urlencode({"item_type": item_type})
        return await _call(_company_app(company_id),
                           f"/companies/{int(company_id)}/dms/items/tree?{qs}")

    @mcp.tool()
    async def pennylaneged_create_folder(company_id: int, name: str,
                                         parent_id: Optional[int] = None) -> dict:
        """Crée un dossier dans la GED d'une société.

        Args:
            company_id: id de la société.
            name: nom du dossier (sous sa forme finale — pas de rename séparé ensuite).
            parent_id: id du dossier parent (None = racine de la GED).

        Renvoie le `DmsFolder` créé (dont son `id`, à réutiliser comme `parent_id`).
        """
        item: dict = {"name": name}
        if parent_id is not None:
            item["parent_id"] = int(parent_id)
        return await _call(_company_app(company_id),
                           f"/companies/{int(company_id)}/dms/items", "POST",
                           {"dms_items": [item]})

    # --- Upload (control plane ; octets PUT en LOCAL, jamais par Oto) --------
    @mcp.tool()
    async def pennylaneged_request_upload(
        company_id: int, filename: str, content_type: str,
        byte_size: int, checksum: str,
    ) -> dict:
        """Étape 1/2 d'un upload GED — demande une **URL S3 présignée** (control plane).

        ⚠️ Ne lit PAS le fichier (RGPD : les octets ne transitent JAMAIS par Oto).
        Calcule EN LOCAL, AVANT cet appel : `byte_size` (taille) et `checksum` (MD5 du
        fichier, encodé **base64**). Tape `direct_uploads` (ActiveStorage) et renvoie
        `{signed_id, put_url, put_headers}`.

        Puis, EN LOCAL (pas via MCP, pas par Oto) : **PUT** les octets du fichier
        directement sur `put_url` en passant `put_headers` (Content-Type, Content-MD5).
        Enfin appelle `pennylaneged_finalize(company_id, name, signed_id, parent_id)`.

        Args:
            company_id: id de la société.
            filename: nom du fichier source.
            content_type: type MIME (ex. `application/pdf`).
            byte_size: taille du fichier en octets (calculée en local).
            checksum: MD5 du fichier encodé en base64 (calculé en local).
        """
        res = await _call(
            _company_app(company_id),
            f"/companies/{int(company_id)}/direct_uploads", "POST",
            {"blob": {"filename": filename, "content_type": content_type,
                      "byte_size": int(byte_size), "checksum": checksum}})
        direct = res.get("direct_upload") or {}
        signed_id = res.get("signed_id")
        put_url = direct.get("url")
        if not signed_id or not put_url:
            raise _err(f"Réponse direct_uploads inattendue : {str(res)[:200]}",
                       code=INTERNAL_ERROR)
        return {"signed_id": signed_id, "put_url": put_url,
                "put_headers": direct.get("headers") or {}}

    @mcp.tool()
    async def pennylaneged_finalize(company_id: int, name: str, signed_id: str,
                                    parent_id: Optional[int] = None) -> dict:
        """Étape 2/2 d'un upload GED — crée l'entrée DMS depuis un `signed_id` (control plane).

        À appeler APRÈS avoir PUT les octets en local sur l'`put_url` (cf.
        `pennylaneged_request_upload`). Le `name` est le nom **final** dans la GED
        (renommage standardisé = ce champ, pas d'appel rename séparé).

        Args:
            company_id: id de la société.
            name: nom final du fichier dans la GED.
            signed_id: `signed_id` renvoyé par `pennylaneged_request_upload`.
            parent_id: id du dossier cible (None = racine).

        Renvoie le `DmsFile` créé.
        """
        item: dict = {"name": name, "file": signed_id}
        if parent_id is not None:
            item["parent_id"] = int(parent_id)
        return await _call(_company_app(company_id),
                           f"/companies/{int(company_id)}/dms/items", "POST",
                           {"dms_items": [item]})

    @mcp.tool()
    async def pennylaneged_delete(company_id: int, item_id: int) -> dict:
        """Supprime un item (dossier ou fichier) de la GED d'une société.

        ⚠️ Suppression — n'appeler qu'après confirmation. Un dossier supprimé emporte
        son contenu.

        Args:
            company_id: id de la société.
            item_id: id de l'item DMS à supprimer (cf. `pennylaneged_tree`).
        """
        return await _call(_company_app(company_id),
                           f"/companies/{int(company_id)}/dms/items/{int(item_id)}",
                           "DELETE")
