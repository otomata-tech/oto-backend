"""Brevo — automations (marketing workflows) via l'API PRIVÉE de l'éditeur.

⚠️ API privée non documentée : `workflow-apis.brevo.com/v1`, auth = **session
navigateur vivante** (cookie `auth` httpOnly). Reverse-engineerée depuis l'éditeur
v5 (exploration o-browser du 2026-06-24). Peut casser sans préavis côté Brevo. À NE
PAS confondre avec l'API PUBLIQUE v3 (`api.brevo.com/v3`, clé `api-key`) qui gère
transactionnel / contacts / campagnes — mais PAS l'authoring d'automations (d'où ce
connecteur séparé).

Exécution — **Browserbase** (`oto_mcp/browserbase.py`). Le token Brevo n'est accepté
que depuis une **session navigateur vivante** ; un `httpx`/curl brut est rejeté (403),
et une session **ne se transplante pas** par export de cookie. On loue donc un Chrome
distant : l'utilisateur se logue UNE fois via la **Live View** (il gère SSO/captcha/2FA),
sa session persiste dans un **Context** Browserbase (= le credential per-user, coffre
`brevo`), et chaque appel `workflow-apis` s'exécute en `fetch()` DANS une session
éphémère du Context (cf. `browserbase.run_fetch`). Prouvé 200 le 2026-06-24. Creds
plateforme = env `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID`.

Surface = endpoints **vérifiés empiriquement** :
- onboarding : `brevo_connect_start` (→ Live View) / `brevo_connect_status` (persiste) ;
- lecture : `listing`, workflow complet (triggers + steps + câblage), catalogue ;
- écriture : créer / configurer / supprimer trigger & step (avec `prev` +
  `condition_node` + `next_steps`), activer.

NON exposé (API distincte lourde) : la **création d'un template d'email**
(`/editor-api/*` + `/email/templates/{id}`). Un step `send_email` référence un
`template_id` existant ; le contenu se conçoit dans l'UI Brevo (ou via l'API v3).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR

from .. import access, browserbase, db
from ..auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)

# Couple (API privée, page d'origine) propre à Brevo — passé au substrat générique
# `browserbase.run_fetch`. Le `fetch` est same-origin avec l'app (app.brevo.com)
# pour porter le cookie de session ; l'API workflow-apis.* est un sous-domaine de
# brevo.com joignable avec ce cookie.
_API = "https://workflow-apis.brevo.com/v1"
_APP = "https://app.brevo.com/"


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
    """Context Browserbase de l'utilisateur (= sa session Brevo loguée), résolu du
    coffre. Lève une McpError actionnable si Brevo n'est pas connecté."""
    try:
        return access.resolve_credential("brevo", want="byo").key
    except McpError:
        raise _err("Brevo non connecté. Lance `brevo_connect_start` pour te loguer "
                   "(une fois) via la Live View.")


async def _api(method: str, path: str, body: Optional[dict] = None) -> dict:
    """Exécute un appel `workflow-apis` dans la session Browserbase de l'user.
    Renvoie le `data` décodé. Lève une McpError actionnable sinon."""
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
        raise _err("Session Brevo expirée / déconnectée — relance `brevo_connect_start`.")
    if not (200 <= (st or 0) < 300):
        raise _err(f"Brevo a renvoyé {st} : {str(res.get('data'))[:200]}", code=INTERNAL_ERROR)
    return res["data"]


def register(mcp: FastMCP) -> None:

    # --- Onboarding (Live View) --------------------------------------------
    @mcp.tool()
    async def brevo_connect_start(ctx: Context) -> dict:
        """Démarre la connexion à Brevo (automations). Ouvre un navigateur distant
        et renvoie une **`live_view_url`** : ouvre-la, connecte-toi à Brevo
        normalement (email/mot de passe, Google SSO, captcha — tu gères tout dans
        cette fenêtre). Puis appelle `brevo_connect_status(context_id, session_id)`
        avec les valeurs renvoyées pour finaliser (ta session est mémorisée ; à
        refaire seulement quand elle expire).
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
            "instructions": "Ouvre `live_view_url`, connecte-toi à Brevo, puis appelle "
                            "`brevo_connect_status` avec context_id + session_id.",
        }

    @mcp.tool()
    async def brevo_connect_status(ctx: Context, context_id: str,
                                   session_id: str) -> dict:
        """Finalise la connexion Brevo. Vérifie que tu t'es bien logué dans la Live
        View ; si oui, **mémorise** ta session (le Context) pour les prochains
        appels. Renvoie `{connected}`. Rappelle-le si `connected=false` (pas encore
        logué)."""
        sub = _sub()
        from patchright.async_api import async_playwright
        authed = False
        try:
            async with async_playwright() as p:
                b = await p.chromium.connect_over_cdp(browserbase.connect_url(session_id))
                c = b.contexts[0] if b.contexts else await b.new_context()
                cks = await c.cookies()
                authed = any(x["name"] == "auth" for x in cks)
                await b.close()
        except Exception as e:
            raise _err(f"Impossible de vérifier la session ({e}).", code=INTERNAL_ERROR)
        if not authed:
            return {"connected": False,
                    "hint": "Pas encore logué — connecte-toi dans la Live View puis relance."}
        browserbase.release_session(session_id)  # persiste le Context
        db.set_user_api_key(sub, "brevo", context_id)
        return {"connected": True, "context_id": context_id}

    # --- Lecture ------------------------------------------------------------
    @mcp.tool()
    async def brevo_list_automations(ctx: Context) -> dict:
        """Liste les automations (scénarios marketing) du compte Brevo connecté.

        Renvoie `workflows[]` avec `id`, `scenario_name`, `status`, `created_at`/
        `updated_at`. Utilise l'`id` avec `brevo_get_automation`.
        """
        return await _api("GET", "/workflow/listing")

    @mcp.tool()
    async def brevo_get_automation(ctx: Context, workflow_id: int) -> dict:
        """Structure complète d'une automation : triggers (portes d'entrée), steps
        (étapes, MAP keyée par id) et le câblage du graphe (`next`/`prev`,
        `is_condition`, `condition_node`). Inclut le DSL compilé des conditions
        (`fe_query` / `dsl`).
        """
        return await _api("GET", f"/workflow/{int(workflow_id)}")

    @mcp.tool()
    async def brevo_automation_catalog(ctx: Context, workflow_id: int = 0) -> dict:
        """Catalogue des triggers disponibles (palette de l'éditeur), groupés par
        source (contacts / email / WhatsApp…). Chaque entrée porte son
        `internal_action_id`, `action_type`, label — à passer à `brevo_add_trigger`
        / `brevo_add_step`.
        """
        wid = int(workflow_id) or 1
        return await _api("GET", f"/workflow/getCategoryData?workflow_id={wid}")

    # --- Création -----------------------------------------------------------
    @mcp.tool()
    async def brevo_create_automation(ctx: Context, name: str,
                                      description: str = "") -> dict:
        """Crée un scénario d'automation VIDE et renvoie `{workflow_id}`.

        Étape 1 du build : ensuite `brevo_add_trigger` (porte d'entrée), puis
        `brevo_add_step` + `brevo_configure_step`, puis `brevo_set_status('active')`.
        """
        if not (name or "").strip():
            raise _err("`name` est requis.")
        return await _api("POST", "/workflow/createcustom", {
            "workflow_name": name.strip(), "workflow_desc": description or "",
            "multiple_trigger": False, "is_default": True,
        })

    @mcp.tool()
    async def brevo_add_trigger(ctx: Context, workflow_id: int, trigger_name: str,
                                internal_action_id: int, source: str = "contacts") -> dict:
        """Ajoute une porte d'entrée (trigger) à un scénario. Renvoie
        `{start_point_id}`. `trigger_name`/`internal_action_id`/`source` viennent de
        `brevo_automation_catalog` (ex. segment = `contact_match_one_segment`, id 19,
        source `contacts`). La condition fine se règle ensuite via
        `brevo_configure_trigger`.
        """
        return await _api("POST", f"/workflow/{int(workflow_id)}/trigger?platform=web", {
            "trigger_name": trigger_name, "multiple_entry": False,
            "internal_action_id": int(internal_action_id), "source": source,
        })

    @mcp.tool()
    async def brevo_add_step(ctx: Context, workflow_id: int, step_type: str,
                             internal_action_id: int, is_condition: bool = False,
                             prev: Optional[int] = None, next: int = 0,
                             condition_node: Optional[str] = None,
                             source: str = "contacts") -> dict:
        """Ajoute une étape (action ou condition) et renvoie `{step_id}`. Câblage :
        `prev` = id du nœud précédent ; pour brancher SOUS une condition, `prev` = id
        du nœud condition + `condition_node` = "0" (oui) / "1" (non) ;
        `is_condition=True` pour un nœud de branche (ex. `if_else_bool_segmentation`,
        id 18). Crée le nœud SANS sa config (→ `brevo_configure_step`).
        """
        body: dict = {
            "next": int(next), "prev": (int(prev) if prev is not None else None),
            "type": step_type, "internal_action_id": int(internal_action_id),
            "is_condition": bool(is_condition), "source": source,
        }
        if condition_node is not None:
            body["condition_node"] = str(condition_node)
        return await _api("POST", f"/workflow/{int(workflow_id)}/step?platform=web", body)

    # --- Configuration ------------------------------------------------------
    @mcp.tool()
    async def brevo_configure_step(ctx: Context, workflow_id: int, step_id: int,
                                   step_name: str, internal_action_id: int,
                                   config: dict, is_condition: bool = False,
                                   source: Optional[str] = None,
                                   next_steps: Optional[list] = None) -> dict:
        """Configure une étape déjà créée (le write qui porte la donnée réelle).
        `config` = le bloc de réglage, sous une clé nommée `step_name`. Exemples :
        - **attente** : `step_name="wait_until"`, id 21,
          `config={"wait_for":[{"unit":"Hours","delay":"2"}]}` ;
        - **email** : `step_name="send_email"`, id 1, `source="messaging"`,
          `config={"template_id":<id existant>,"subject":"…","from_name":"…",
          "from_email":"…","preview_text":"…"}` ;
        - **condition** : `step_name="if_else_bool_segmentation"`, id 18,
          `is_condition=True`, `config={"branches":[{"fe_query":"<DSL json string>"},
          {"is_last_branch":True}]}` + **`next_steps=[<step branche oui>,<step branche
          non>]`** (câblage des sorties).
        Le `send_email` référence un `template_id` **existant** (création de template
        = API distincte, non exposée).
        """
        body: dict = {
            "step_id": int(step_id), "step_name": step_name, "step_type": "",
            step_name: config, "workflowId": int(workflow_id),
            "internal_action_id": int(internal_action_id),
        }
        if is_condition:
            body["is_condition"] = True
        if source is not None:
            body["source"] = source
        if next_steps is not None:
            body["next_steps"] = next_steps
        return await _api("PUT", f"/workflow/{int(workflow_id)}/step", body)

    @mcp.tool()
    async def brevo_configure_trigger(ctx: Context, workflow_id: int,
                                      trigger_point_id: int, internal_action_id: int,
                                      event_name: str, config: dict,
                                      source: str = "contacts") -> dict:
        """Configure une porte d'entrée déjà ajoutée. `config` = réglages spécifiques
        fusionnés (ex. trigger segment : `config={"segment_id":1,
        "segment_name":"Segment A","is_bulk":True,"schedule":{"interval":"daily",
        "schedule_time":"14:00","timezone":"Europe/Paris"}}`). Renvoie `{status}`."""
        body: dict = {
            "trigger_point_id": int(trigger_point_id), "workflow_id": int(workflow_id),
            "trigger_point_type": "start_workflow",
            "internal_action_id": int(internal_action_id), "source": source,
            "event_name": event_name,
        }
        body.update(config or {})
        return await _api("PUT", "/workflow/update/trigger", body)

    # --- Suppression / activation ------------------------------------------
    @mcp.tool()
    async def brevo_delete_trigger(ctx: Context, workflow_id: int,
                                   trigger_point_id: int) -> dict:
        """Supprime un trigger d'un scénario. Renvoie `{status}`."""
        return await _api("DELETE", "/workflow/trigger", {
            "trigger_point_id": int(trigger_point_id), "workflow_id": int(workflow_id)})

    @mcp.tool()
    async def brevo_delete_step(ctx: Context, workflow_id: int, step_id: int) -> dict:
        """Supprime une étape d'un scénario. Renvoie `{status}`."""
        return await _api("DELETE", f"/workflow/{int(workflow_id)}/step",
                          {"step_id": int(step_id)})

    @mcp.tool()
    async def brevo_set_status(ctx: Context, workflow_id: int,
                               status: str = "active") -> dict:
        """Active / met en pause un scénario. `status` ∈ `active` | `paused` |
        `draft`. À appeler en dernier, tous les nœuds créés ET configurés."""
        st = (status or "").strip().lower()
        if st not in ("active", "paused", "draft"):
            raise _err("`status` doit valoir active | paused | draft.")
        return await _api("PUT", f"/workflow/{int(workflow_id)}/status", {"status": st})
