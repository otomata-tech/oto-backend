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

import os
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


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
        # Logto valide le redirect contre l'app pré-enregistrée : on renvoie le
        # client_id partagé + ce que le client a envoyé.
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
