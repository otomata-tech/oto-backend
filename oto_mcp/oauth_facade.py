"""Façade DCR devant Logto (technique éprouvée sur ytmusic MCP).

claude.ai (et d'autres clients MCP) exigent un `registration_endpoint` RFC 7591
(Dynamic Client Registration). Logto self-hosted n'en a pas → sans ça, l'user
doit coller le `client_id` à la main dans le connecteur (friction rédhibitoire
pour l'onboarding tiers).

On agit en **façade de l'authorization server** : le PRM pointe les clients vers
NOUS (cf. `_build_auth` → authorization_servers = OTO), on sert une métadonnée AS
augmentée (un `registration_endpoint` à nous, tous les autres endpoints = ceux de
Logto), et un endpoint DCR qui renvoie un client Logto **pré-créé partagé**. Les
tokens restent émis et signés par Logto ; on ne fait que les vérifier.

Le redirect URI de claude.ai est fixe et déjà enregistré sur l'app Logto pré-créée
(`Claude (oto MCP)`), donc on peut renvoyer le même `client_id` à chaque
enregistrement sans risque.
"""
from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_log = logging.getLogger("oto_mcp.oauth_facade")


def _logto_issuer() -> str:
    return os.environ["LOGTO_ENDPOINT"].rstrip("/") + "/oidc"


def as_metadata(public_url: str) -> dict:
    """Métadonnée RFC 8414 servie sur NOTRE domaine : issuer = nous, le
    `registration_endpoint` est à nous, tous les endpoints OAuth sont ceux de Logto."""
    issuer = _logto_issuer()
    return {
        "issuer": public_url,
        "authorization_endpoint": f"{issuer}/auth",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/jwks",
        "registration_endpoint": f"{public_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
    }


def _cors() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type",
    }


# ⚠️ INVARIANT DE SÉCURITÉ (audit 2026-06-13). Le client DCR partagé
# (`OTO_MCP_CLAUDE_APP_ID`) n'est sûr QUE parce que l'app Logto correspondante
# n'enregistre QUE des redirect_uris étroits (callbacks claude.ai/.com + locaux).
# Logto valide le redirect au `/authorize` (enforcement réel) ; un redirect large
# ou wildcard ajouté à l'app Logto rendrait le DCR ouvert + client public
# exploitable (vol de code d'autorisation). On valide AUSSI ici (défense en
# profondeur, fail-fast à l'enregistrement) — sans dépendre uniquement de Logto.
# Hôtes claude.ai/.com en https (callback MCP) + hôtes locaux en http (Claude
# Code/desktop, port ignoré). Comparaison de host EXACTE après parsing : jamais
# de startswith sur l'URL brute (contournable via `http://localhost.evil.com`).
_ALLOWED_HTTPS_HOSTS = {"claude.ai", "claude.com"}
_ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CALLBACK_PATH = "/api/mcp/auth_callback"


def _extra_https_hosts() -> set[str]:
    extra = (os.environ.get("OTO_MCP_DCR_ALLOWED_REDIRECTS") or "").strip()
    return {h.strip().lower().rstrip(".") for h in extra.split(",") if h.strip()}


def _redirect_ok(uri: str) -> bool:
    if not isinstance(uri, str):
        return False
    try:
        p = urlparse(uri)
    except Exception:
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if p.scheme == "https" and host in (_ALLOWED_HTTPS_HOSTS | _extra_https_hosts()) \
            and p.path.startswith(_CALLBACK_PATH):
        return True
    # ChatGPT (connecteurs MCP) : redirect `https://chatgpt.com/connector/oauth/<id>`
    # où <id> est propre au connecteur (varie) → on matche le préfixe de path, pas
    # l'URI exacte. Garde-fou réel = l'app Logto (redirect enregistré, exact).
    if p.scheme == "https" and host == "chatgpt.com" \
            and p.path.startswith("/connector/oauth/"):
        return True
    # Mistral (Le Chat, connecteurs MCP) : redirect FIXE callback.mistral.ai.
    if p.scheme == "https" and host == "callback.mistral.ai" \
            and p.path.startswith("/v1/integrations_auth/"):
        return True
    if p.scheme == "http" and host in _ALLOWED_LOCAL_HOSTS:
        return True
    return False


# ── DCR réelle : enregistrement dynamique du redirect dans l'app Logto ────────
# Le client_id reste partagé, mais on ÉTEND la liste de redirectUris de l'app
# Logto à chaque DCR (le redirect de ChatGPT est propre à chaque connecteur →
# impossible à pré-enregistrer). Ainsi N'IMPORTE QUEL user installe sans
# intervention manuelle. Les redirects sont déjà validés par _redirect_ok
# (host allowlist) → on n'enregistre QUE des callbacks légitimes.
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"  # vs WAF Cloudflare (1010)
_MGMT_RESOURCE = "https://default.logto.app/api"
_mgmt_tok = {"value": None, "exp": 0.0}


def _logto_base() -> str:
    return os.environ["LOGTO_ENDPOINT"].rstrip("/")


def _mgmt_token() -> str:
    import requests
    cid = os.environ.get("OTO_MCP_LOGTO_M2M_ID")
    csec = os.environ.get("OTO_MCP_LOGTO_M2M_SECRET")
    if not cid or not csec:
        raise RuntimeError("M2M Logto non configuré (OTO_MCP_LOGTO_M2M_ID/SECRET)")
    now = time.time()
    if _mgmt_tok["value"] and _mgmt_tok["exp"] > now + 30:
        return _mgmt_tok["value"]
    r = requests.post(
        f"{_logto_base()}/oidc/token",
        data={"grant_type": "client_credentials", "resource": _MGMT_RESOURCE, "scope": "all"},
        auth=(cid, csec), headers={"User-Agent": _UA}, timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    _mgmt_tok["value"] = j["access_token"]
    _mgmt_tok["exp"] = now + int(j.get("expires_in", 3600))
    return _mgmt_tok["value"]


def _register_redirects(app_id: str, redirect_uris: list) -> None:
    """Ajoute les `redirect_uris` (déjà validés) à l'app Logto partagée (dédup) +
    l'origine CORS https correspondante. Idempotent ; no-op si tout est déjà là.
    Lève si la Management API échoue (l'appelant décide quoi en faire)."""
    import requests
    base, tok = _logto_base(), _mgmt_token()
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA, "Content-Type": "application/json"}
    data = requests.get(f"{base}/api/applications/{app_id}", headers=h, timeout=15)
    data.raise_for_status()
    app = data.json()
    meta = app.get("oidcClientMetadata", {}) or {}
    custom = app.get("customClientMetadata", {}) or {}
    cur = list(meta.get("redirectUris", []))
    cors = list(custom.get("corsAllowedOrigins", []))
    new = [u for u in redirect_uris if u not in cur]
    for u in redirect_uris:
        pp = urlparse(u)
        origin = f"{pp.scheme}://{pp.netloc}"
        if pp.scheme == "https" and origin not in cors:
            cors.append(origin)
    if not new and set(cors) == set(custom.get("corsAllowedOrigins", []) or []):
        return
    meta["redirectUris"] = cur + new
    custom["corsAllowedOrigins"] = cors
    p = requests.patch(f"{base}/api/applications/{app_id}",
                       json={"oidcClientMetadata": meta, "customClientMetadata": custom},
                       headers=h, timeout=15)
    p.raise_for_status()
    _log.info("DCR: app %s — +%d redirect(s), cors=%s", app_id, len(new), cors)


def make_routes(public_url: str, claude_app_id: str) -> list[Route]:
    public_url = public_url.rstrip("/")

    async def as_meta(request: Request) -> JSONResponse:
        return JSONResponse(as_metadata(public_url))

    async def dcr(request: Request) -> JSONResponse:
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=_cors())
        try:
            body = await request.json()
        except Exception:
            body = {}
        # Défense en profondeur (audit 2026-06-13) : on n'émet le client_id
        # partagé QUE pour des redirect_uris connus. L'enforcement réel reste
        # Logto au `/authorize` (cf. invariant ci-dessus), mais fail-fast ici
        # évite de tendre un client public à un redirect non prévu.
        requested = body.get("redirect_uris") or []
        if not isinstance(requested, list) or any(not _redirect_ok(u) for u in requested):
            _log.warning("DCR refusé — redirect_uris=%r client_name=%r grant_types=%r",
                         requested, body.get("client_name"), body.get("grant_types"))
            return JSONResponse(
                {"error": "invalid_redirect_uri",
                 "error_description": "redirect_uri non autorisé pour ce serveur"},
                status_code=400,
                headers=_cors(),
            )
        # DCR réelle : on enregistre dynamiquement le(s) redirect(s) dans l'app
        # Logto partagée (le redirect de ChatGPT est propre à chaque connecteur).
        # Fail-OPEN : si la Management API échoue, on renvoie quand même le
        # client_id — Claude reste fonctionnel (son redirect est déjà enregistré) ;
        # seul un NOUVEAU redirect (ChatGPT) serait alors refusé plus tard au
        # /authorize, cas dégradé loggé, jamais une régression de l'existant.
        try:
            _register_redirects(claude_app_id, requested)
        except Exception:
            _log.exception("DCR: enregistrement Logto échoué (redirects=%r)", requested)
        # Logto valide le redirect contre l'app : on renvoie le client_id partagé.
        return JSONResponse(
            {
                "client_id": claude_app_id,
                "client_id_issued_at": int(time.time()),
                "redirect_uris": body.get("redirect_uris", []),
                "token_endpoint_auth_method": "none",
                "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
                "response_types": body.get("response_types", ["code"]),
                "client_name": body.get("client_name"),
            },
            status_code=201,
            headers=_cors(),
        )

    return [
        # Métadonnée AS servie à TOUTES les variantes de chemin que les clients
        # MCP tentent : racine (issuer sans path) ET path-suffixée par la
        # ressource `/mcp` (RFC 8414 path-insertion — claude.ai essaie les deux).
        Route("/.well-known/oauth-authorization-server", as_meta, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/mcp", as_meta, methods=["GET"]),
        Route("/oauth/register", dcr, methods=["POST", "OPTIONS"]),
    ]
