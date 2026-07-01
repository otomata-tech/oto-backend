"""Routes REST OAuth Folk — fédération du MCP officiel de Folk per-user (#85).

Flow web (calqué sur api_routes_atlassian.py) :
- `GET    /api/folk/oauth/start`    (auth Logto) → {auth_url} à ouvrir
- `GET    /api/folk/oauth/callback` (no auth, Folk redirige) → exchange + persist
- `GET    /api/folk/oauth/status`   (auth) → {connected, set_at}
- `DELETE /api/folk/oauth`          (auth) → déconnecte

Le token per-user est stocké dans le coffre (connector='folkmcp') ; le proxy de
tools/mount.py l'injecte par requête (access.resolve_mount_token → refresh). Ne
concerne QUE le connecteur fédéré `folkmcp` — le connecteur natif `folk` (clé API)
n'a pas d'OAuth.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import folk_oauth

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
        return json_response(request, {"auth_url": folk_oauth.build_auth_url(sub)})

    async def callback(request: Request) -> Response:
        # Folk (Stytch) redirige ici (pas d'auth Logto) ; l'identité vient du state signé.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        parsed = folk_oauth.verify_state(state) if state else None
        if not code or not parsed:
            return RedirectResponse(f"{_app_url()}/?folk=error", status_code=302)
        sub, verifier_pkce = parsed
        try:
            tokens = folk_oauth.exchange_code(code, verifier_pkce)
            folk_oauth.persist_token(sub, tokens)
        except Exception:
            return RedirectResponse(f"{_app_url()}/?folk=error", status_code=302)
        return RedirectResponse(f"{_app_url()}/?folk=connected", status_code=302)

    async def status(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, folk_oauth.status_for(sub))

    async def disconnect(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, {"ok": True, "disconnected": folk_oauth.disconnect(sub)})

    return [
        Route("/api/folk/oauth/start", start, methods=["GET"]),
        Route("/api/folk/oauth/start", options_handler, methods=["OPTIONS"]),
        Route("/api/folk/oauth/callback", callback, methods=["GET"]),
        Route("/api/folk/oauth/status", status, methods=["GET"]),
        Route("/api/folk/oauth/status", options_handler, methods=["OPTIONS"]),
        Route("/api/folk/oauth", disconnect, methods=["DELETE"]),
        Route("/api/folk/oauth", options_handler, methods=["OPTIONS"]),
    ]
