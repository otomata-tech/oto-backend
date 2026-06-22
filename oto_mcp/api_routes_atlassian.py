"""Routes REST OAuth Atlassian — fédération du Rovo Remote MCP per-user (#40).

Flow web (calqué sur api_routes_memento.py) :
- `GET    /api/atlassian/oauth/start`    (auth Logto) → {auth_url} à ouvrir
- `GET    /api/atlassian/oauth/callback` (no auth, Atlassian redirige) → exchange + persist
- `GET    /api/atlassian/oauth/status`   (auth) → {connected, set_at}
- `DELETE /api/atlassian/oauth`          (auth) → déconnecte

Le token per-user est stocké dans le coffre (connector='atlassian') ; le proxy
de tools/mount.py l'injecte par requête (access.resolve_mount_token → refresh).
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import atlassian_oauth

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _app_url() -> str:
        return os.environ.get("OTO_APP_URL", "https://dashboard.oto.ninja").rstrip("/")

    async def start(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, {"auth_url": atlassian_oauth.build_auth_url(sub)})

    async def callback(request: Request) -> Response:
        # Atlassian redirige ici (pas d'auth Logto) ; l'identité vient du state signé.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        parsed = atlassian_oauth.verify_state(state) if state else None
        if not code or not parsed:
            return RedirectResponse(f"{_app_url()}/?atlassian=error", status_code=302)
        sub, verifier_pkce = parsed
        try:
            tokens = atlassian_oauth.exchange_code(code, verifier_pkce)
            atlassian_oauth.persist_token(sub, tokens)
        except Exception:
            return RedirectResponse(f"{_app_url()}/?atlassian=error", status_code=302)
        return RedirectResponse(f"{_app_url()}/?atlassian=connected", status_code=302)

    async def status(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, atlassian_oauth.status_for(sub))

    async def disconnect(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, {"ok": True, "disconnected": atlassian_oauth.disconnect(sub)})

    return [
        Route("/api/atlassian/oauth/start", start, methods=["GET"]),
        Route("/api/atlassian/oauth/start", options_handler, methods=["OPTIONS"]),
        Route("/api/atlassian/oauth/callback", callback, methods=["GET"]),
        Route("/api/atlassian/oauth/status", status, methods=["GET"]),
        Route("/api/atlassian/oauth/status", options_handler, methods=["OPTIONS"]),
        Route("/api/atlassian/oauth", disconnect, methods=["DELETE"]),
        Route("/api/atlassian/oauth", options_handler, methods=["OPTIONS"]),
    ]
