"""Shim OAuth ANONYME pour les endpoints MCP publics (`<slug>.mcp.oto.cx`, ADR 0032).

Les connecteurs custom de claude.ai / Mistral EXIGENT un flux OAuth (discovery + DCR +
authorize + token) même pour un serveur qui ne demande AUCUNE auth : sans route OAuth,
`.well-known/*` + `/register` renvoient 404 → le client échoue (« impossible de
s'inscrire auprès du service de connexion »). Ce shim satisfait leur flux en
**AUTO-APPROUVANT**, sans le moindre écran de login ni compte créé :
- discovery (`.well-known/*`) + DCR (`/register`) → client_id statique ;
- `/authorize` → **302 immédiat** avec un code (aucun login) ;
- `/token` → délivre un token OPAQUE **sans privilège** (l'app anonyme `/mcp` ne le
  vérifie jamais — elle est sans auth : le token n'ouvre rien).

Sécurité : le seul risque réel est l'open-redirect → `redirect_uri` validé STRICTEMENT
(mêmes hôtes que la façade DCR : claude.ai/.com, chatgpt, mistral). PKCE (S256) respecté,
code à usage unique + TTL court. Mono-process → store in-memory suffisant. Host-aware :
resource/issuer dérivés du Host de la requête (= le sous-domaine servi).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from .oauth_facade import _cors, _redirect_ok

logger = logging.getLogger("oto_mcp.anon_oauth")

_ANON_CLIENT_ID = "oto-anon-public"
_SCOPES = ["mcp", "openid", "profile", "email", "offline_access"]

# code -> (code_challenge, redirect_uri, expiry). Mono-process, borné, TTL court.
_CODES: dict[str, tuple] = {}
_CODE_TTL = 300
_CAP = 10_000


def _host_base(request: Request) -> str:
    host = (request.headers.get("host") or "").split(",")[0].strip()
    return f"https://{host}"


def _as_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": _SCOPES,
    }


def _prm(base: str) -> dict:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": _SCOPES,
        "bearer_methods_supported": ["header"],
    }


def _s256(verifier: str) -> str:
    d = hashlib.sha256((verifier or "").encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def _put_code(code: str, challenge: str, redirect_uri: str) -> None:
    _CODES[code] = (challenge, redirect_uri, time.time() + _CODE_TTL)
    while len(_CODES) > _CAP:
        _CODES.pop(next(iter(_CODES)))


def make_routes() -> list[Route]:
    async def prm(request):
        return JSONResponse(_prm(_host_base(request)))

    async def as_meta(request):
        return JSONResponse(_as_metadata(_host_base(request)))

    async def oidc_meta(request):
        base = _host_base(request)
        return JSONResponse({**_as_metadata(base),
                             "subject_types_supported": ["public"],
                             "id_token_signing_alg_values_supported": ["RS256"]})

    async def register(request):
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=_cors())
        try:
            body = await request.json()
        except Exception:
            body = {}
        return JSONResponse({
            "client_id": _ANON_CLIENT_ID,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": body.get("redirect_uris", []),
            "token_endpoint_auth_method": "none",
            "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
            "response_types": body.get("response_types", ["code"]),
            "client_name": body.get("client_name"),
        }, status_code=201, headers=_cors())

    async def authorize(request):
        p = request.query_params
        redirect_uri = p.get("redirect_uri", "")
        state = p.get("state", "")
        challenge = p.get("code_challenge", "")
        # Anti open-redirect : seul un callback client connu est accepté (aucune donnée
        # sensible en jeu — le token n'ouvre rien — mais on ne devient pas un redirecteur).
        if not _redirect_ok(redirect_uri):
            logger.warning("anon authorize: redirect_uri refusé %r", redirect_uri)
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        code = secrets.token_urlsafe(32)
        _put_code(code, challenge, redirect_uri)  # AUTO-APPROUVE (pas de login)
        sep = "&" if "?" in redirect_uri else "?"
        params = {"code": code}
        if state:
            params["state"] = state
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    async def token(request):
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=_cors())
        form = await request.form()
        grant = form.get("grant_type")
        if grant == "authorization_code":
            rec = _CODES.pop(form.get("code", ""), None)
            if not rec or rec[2] < time.time():
                return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_cors())
            challenge, _redirect_uri, _ = rec
            if challenge and _s256(form.get("code_verifier", "")) != challenge:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE mismatch"},
                                    status_code=400, headers=_cors())
        elif grant != "refresh_token":  # refresh : token sans privilège → ré-émission libre
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=_cors())
        return JSONResponse({
            "access_token": "anon-" + secrets.token_urlsafe(24),
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "anon-" + secrets.token_urlsafe(24),
            "scope": "mcp",
        }, headers=_cors())

    return [
        Route("/.well-known/oauth-protected-resource", prm, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", prm, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", as_meta, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/mcp", as_meta, methods=["GET"]),
        Route("/.well-known/openid-configuration", oidc_meta, methods=["GET"]),
        Route("/.well-known/openid-configuration/mcp", oidc_meta, methods=["GET"]),
        Route("/register", register, methods=["POST", "OPTIONS"]),
        Route("/oauth/register", register, methods=["POST", "OPTIONS"]),
        Route("/authorize", authorize, methods=["GET"]),
        Route("/token", token, methods=["POST", "OPTIONS"]),
    ]
