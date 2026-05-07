"""REST API consommée par le frontend oto.ninja (page de gestion de compte).

Endpoints :
- `GET    /api/me`                       → infos sur l'utilisateur courant
- `POST   /api/settings/linkedin`        → enregistre/met à jour le cookie li_at
- `DELETE /api/settings/linkedin`        → efface le cookie

Auth : Bearer JWT Logto, vérifié avec le même `JWTVerifier` que `/mcp` (le sub
du token = identifiant utilisateur côté DB). Le frontend obtient ce token via
le SDK `@logto/vue` après login OIDC.

CORS : limité aux origines oto.ninja (+ localhost en dev) — l'API n'est pas
publique.
"""
from __future__ import annotations

import os
from typing import Iterable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import db


def _allowed_origins() -> list[str]:
    """Origines autorisées pour CORS — surchargeables via env."""
    raw = os.environ.get("OTO_MCP_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://oto.ninja",
        "https://www.oto.ninja",
        "http://localhost:5173",  # vite dev
        "http://localhost:4173",  # vite preview
    ]


def _cors_headers(origin: str | None) -> dict[str, str]:
    if origin and origin in _allowed_origins():
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
    return {}


async def _authenticate(request: Request, verifier: JWTVerifier) -> tuple[str | None, JSONResponse | None]:
    """Renvoie (sub, None) en cas de succès, sinon (None, 401 response)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None, _json_error(request, 401, "missing_bearer")
    token = auth[7:].strip()
    access = await verifier.verify_token(token)
    if not access or not getattr(access, "claims", None):
        return None, _json_error(request, 401, "invalid_token")
    sub = access.claims.get("sub")
    if not sub:
        return None, _json_error(request, 401, "missing_sub")
    # Synchronise les claims (email/name) à chaque requête authentifiée.
    db.upsert_user(sub, email=access.claims.get("email"), name=access.claims.get("name"))
    return sub, None


def _json_error(request: Request, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": code},
        status_code=status,
        headers=_cors_headers(request.headers.get("origin")),
    )


def _json(request: Request, payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status, headers=_cors_headers(request.headers.get("origin"))
    )


def make_routes(verifier: JWTVerifier) -> Iterable:
    """Construit les Route Starlette à insérer sur l'app HTTP FastMCP."""
    from starlette.routing import Route

    async def options_handler(request: Request) -> Response:
        # Préflight CORS — répond toujours, pas d'auth nécessaire.
        return Response(status_code=204, headers=_cors_headers(request.headers.get("origin")))

    async def me(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        user = db.get_user(sub) or {}
        return _json(request, {
            "sub": sub,
            "email": user.get("email"),
            "name": user.get("name"),
            "linkedin": {
                "configured": bool(user.get("linkedin_cookie")),
                "set_at": user.get("linkedin_cookie_set_at"),
                "user_agent": user.get("linkedin_user_agent"),
            },
        })

    async def linkedin_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        cookie = (body.get("cookie") or "").strip()
        user_agent = (body.get("user_agent") or "").strip() or None
        if not cookie:
            return _json_error(request, 400, "empty_cookie")
        db.set_linkedin_cookie(sub, cookie, user_agent=user_agent)
        return _json(request, {"ok": True})

    async def linkedin_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        db.clear_linkedin_cookie(sub)
        return _json(request, {"ok": True})

    return [
        Route("/api/me", me, methods=["GET"]),
        Route("/api/me", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/linkedin", linkedin_save, methods=["POST"]),
        Route("/api/settings/linkedin", linkedin_clear, methods=["DELETE"]),
        Route("/api/settings/linkedin", options_handler, methods=["OPTIONS"]),
    ]
