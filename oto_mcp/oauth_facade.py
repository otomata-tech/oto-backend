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

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_log = logging.getLogger("oto_mcp.oauth_facade")


def _logto_issuer() -> str:
    return os.environ["LOGTO_ENDPOINT"].rstrip("/") + "/oidc"


def as_metadata(public_url: str) -> dict:
    """Métadonnée RFC 8414 servie sur NOTRE domaine : issuer = nous, le
    `registration_endpoint` est à nous, tous les endpoints OAuth sont ceux de Logto.

    ⚠️ RFC 8414 §3.3 : l'`issuer` retourné DOIT être IDENTIQUE à l'identifiant d'AS
    que le client a annoncé dans le PRM (`authorization_servers`) et dans lequel il
    a inséré le chemin well-known. Le PRM passe `public_base` par `AnyHttpUrl`
    (RemoteAuthProvider, server.py), qui NORMALISE en ajoutant un slash final
    (`https://x` → `https://x/`). On normalise l'issuer par le MÊME `AnyHttpUrl` →
    égalité byte-à-byte garantie. Sans ça, un client strict (Mistral) rejette le
    discovery pour issuer mismatch (claude.ai, lui, tolère le slash). Vécu 2026-06-25."""
    logto = _logto_issuer()
    return {
        "issuer": str(AnyHttpUrl(public_url)),
        "authorization_endpoint": f"{logto}/auth",
        "token_endpoint": f"{logto}/token",
        "jwks_uri": f"{logto}/jwks",
        "registration_endpoint": f"{public_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
    }


def as_oidc_metadata(public_url: str) -> dict:
    """OIDC Discovery 1.0 servie sur NOTRE domaine (`/.well-known/openid-configuration`).

    Certains clients OAuth 2.1 (dont Mistral) sondent l'OIDC discovery EN PLUS de
    RFC 8414 (`oauth-authorization-server`) ; un 404 ici peut casser leur résolution
    d'AS. On réutilise `as_metadata` et on ajoute les champs OIDC OBLIGATOIRES
    (`subject_types_supported`, `id_token_signing_alg_values_supported` = ES384, ce
    que Logto self-hosted signe) + `userinfo_endpoint`. Même issuer (normalisé) →
    pas de mismatch."""
    logto = _logto_issuer()
    return {
        **as_metadata(public_url),
        "userinfo_endpoint": f"{logto}/me",
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["ES384"],
        "claims_supported": ["sub", "iss", "aud", "exp", "iat", "email", "name"],
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


def logto_user_primary_email(sub: str) -> str | None:
    """Email primaire AUTORITATIF d'un user Logto (Management API). Dans Logto, le
    `primaryEmail` n'est posé qu'après vérification de l'adresse → sa présence vaut
    « email vérifié », et c'est la SOURCE DE VÉRITÉ (un claim de token, lui, peut
    mentir). Utilisé par la bascule de tenant pour décider d'un merge de comptes sans
    faire confiance au token. Renvoie None si user inconnu / Logto indispo (l'appelant
    ne migre alors PAS — fail-safe)."""
    import requests
    try:
        base, tok = _logto_base(), _mgmt_token()
        r = requests.get(
            f"{base}/api/users/{sub}",
            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("primaryEmail") or None
    except Exception as e:
        _log.warning("lookup primaryEmail Logto échoué pour %s : %s", sub, e)
        return None


# ── Magic link : one-time-token Logto (onboarding sans saisie de code) ────────
# Le backend mint un OTT pour l'email de l'invité (Management API) ; le lien le
# porte → la custom UI Logto le consomme (signIn extraParams) → auth silencieuse,
# compte créé/loggé avec l'email EXACT (pas de mismatch). Best-effort : si le mint
# échoue (M2M absent, Logto down), l'invitation reste valide via le flow code email.
def mint_one_time_token(email: str, *, expires_in: int = 7 * 24 * 3600) -> str | None:
    """Mint un one-time-token Logto (magic link) pour `email`. Renvoie le token,
    ou None si le mint échoue (l'appelant dégrade vers le lien sans magic-link)."""
    import requests
    try:
        base, tok = _logto_base(), _mgmt_token()
        r = requests.post(
            f"{base}/api/one-time-tokens",
            json={"email": email, "expiresIn": int(expires_in)},
            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA,
                     "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:  # M2M non configuré, Logto indispo, expiry rejeté…
        _log.warning("OTT mint échoué pour %s : %s", email, e)
        return None


def magic_url(base_url: str, email: str, *, expires_in: int = 7 * 24 * 3600) -> str:
    """Augmente `base_url` d'un magic-link Logto pour `email` (params `otl` +
    `login_hint`). Le front fait `signIn({extraParams:{one_time_token: otl},
    loginHint})`. Si le mint échoue, renvoie `base_url` inchangé (dégradation
    gracieuse vers le flow code email)."""
    from urllib.parse import quote
    ott = mint_one_time_token(email, expires_in=expires_in)
    if not ott:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}otl={quote(ott, safe='')}&login_hint={quote(email, safe='')}"


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


def ensure_api_resource(indicator: str, *, name: str | None = None) -> None:
    """Enregistre (idempotent) une API resource Logto pour `indicator` (le resource
    indicator = l'audience JWT, ex. `https://<slug>.mcp.oto.cx/mcp`). Sans ça, Logto
    émet un token OPAQUE pour ce sous-domaine (≠ resource enregistrée) → `invalid_token`
    (blocage historique #44). Best-effort côté appelant : lève si la Management API
    échoue (l'appelant loggue et n'empêche pas la publication). Réutilise le M2M partagé."""
    import requests
    base, tok = _logto_base(), _mgmt_token()
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA, "Content-Type": "application/json"}
    existing = requests.get(f"{base}/api/resources", headers=h, timeout=15)
    existing.raise_for_status()
    if any((r.get("indicator") == indicator) for r in existing.json()):
        return
    r = requests.post(
        f"{base}/api/resources",
        json={"name": name or indicator, "indicator": indicator},
        headers=h, timeout=15,
    )
    r.raise_for_status()
    _log.info("Logto API resource créée : %s", indicator)


# ── PRM (RFC 9728) host-aware — la SEULE pièce de discovery à rendre host-aware ─
# Le PRM annonce le `resource` (= l'audience que le client demandera à Logto). Sur le
# sous-domaine d'un projet org publié, il DOIT annoncer le sous-domaine lui-même (sinon
# claude.ai reçoit un token opaque, blocage #44). L'AS reste canonique (RFC 8707 :
# resource indicator ≠ authorization server) → as_meta/oidc_meta INCHANGÉS.
# Sécurité canonique : on construit le PRM via le MÊME modèle mcp lib que fastmcp, avec
# les MÊMES paramètres pour le host canonique → sortie identique byte-à-byte ; on ne
# diverge le `resource` que pour un sous-domaine org VÉRIFIÉ publié.
def _prm_handler(public_url: str, resource_url: str):
    from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
    from mcp.shared.auth import ProtectedResourceMetadata
    md = ProtectedResourceMetadata(
        resource=AnyHttpUrl(resource_url),
        authorization_servers=[AnyHttpUrl(public_url)],
        # Mêmes valeurs que _build_auth (RemoteAuthProvider) → PRM canonique identique.
        scopes_supported=["openid", "profile", "email", "offline_access"],
        resource_name="oto MCP",
    )
    return ProtectedResourceMetadataHandler(md)


def make_routes(public_url: str, claude_app_id: str) -> list[Route]:
    public_url = public_url.rstrip("/")

    async def as_meta(request: Request) -> JSONResponse:
        return JSONResponse(as_metadata(public_url))

    async def oidc_meta(request: Request) -> JSONResponse:
        return JSONResponse(as_oidc_metadata(public_url))

    async def prm(request: Request):
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        # Sous-domaine d'un projet org PUBLIÉ → resource = ce sous-domaine ; sinon
        # canonique (identique à fastmcp). valid_org_audience = motif + existence DB.
        candidate = f"https://{host}/mcp"
        from . import subdomain_project
        resource_url = candidate if subdomain_project.valid_org_audience(candidate) \
            else f"{public_url}/mcp"
        return await _prm_handler(public_url, resource_url).handle(request)

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
        # OIDC discovery (en plus de RFC 8414) — sondé par les clients OAuth 2.1/OIDC
        # (Mistral) ; un 404 ici peut casser leur résolution d'AS. Mêmes 2 variantes.
        Route("/.well-known/openid-configuration", oidc_meta, methods=["GET"]),
        Route("/.well-known/openid-configuration/mcp", oidc_meta, methods=["GET"]),
        # PRM host-aware (RFC 9728) : shadow les routes fastmcp (insérées avant → priorité).
        # Canonique = identique à fastmcp ; sous-domaine org publié = resource = le sous-domaine.
        Route("/.well-known/oauth-protected-resource", prm, methods=["GET", "OPTIONS"]),
        Route("/.well-known/oauth-protected-resource/mcp", prm, methods=["GET", "OPTIONS"]),
        Route("/oauth/register", dcr, methods=["POST", "OPTIONS"]),
    ]
