"""Routes REST OAuth memento — fédération MCP per-user (otomata#16, B2).

Flow web (calqué sur les routes Google OAuth d'api_routes_datastore.py) :
- `GET    /api/memento/oauth/start`    (auth Logto) → {auth_url} à ouvrir
- `GET    /api/memento/oauth/callback` (no auth, Supabase redirige) → exchange + persist
- `GET    /api/memento/oauth/status`   (auth) → {connected, set_at}
- `DELETE /api/memento/oauth`          (auth) → déconnecte
- `GET    /api/memento/workspaces`     (auth) → topologie des KB
- `GET    /api/memento/pages`          (auth) → pages (documents) d'une KB (browse)
- `GET    /api/memento/document`       (auth) → contenu d'une page (blocs)

Le token per-user est stocké dans le coffre (connector='memento') ; le proxy
de tools/mount.py l'injecte par requête (access.resolve_mount_token → refresh).
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import memento_oauth

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
        return json_response(request, {"auth_url": memento_oauth.build_auth_url(sub)})

    async def callback(request: Request) -> Response:
        # Supabase redirige ici (pas d'auth Logto) ; l'identité vient du state signé.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        parsed = memento_oauth.verify_state(state) if state else None
        if not code or not parsed:
            return RedirectResponse(f"{_app_url()}/?memento=error", status_code=302)
        sub, verifier_pkce = parsed
        try:
            tokens = memento_oauth.exchange_code(code, verifier_pkce)
            memento_oauth.persist_token(sub, tokens)
        except Exception:
            return RedirectResponse(f"{_app_url()}/?memento=error", status_code=302)
        return RedirectResponse(f"{_app_url()}/?memento=connected", status_code=302)

    async def status(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, memento_oauth.status_for(sub))

    async def disconnect(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, {"ok": True, "disconnected": memento_oauth.disconnect(sub)})

    async def workspaces(request: Request) -> JSONResponse:
        # Carte read-only des KB (orientation) ; la curation reste sur me.mento.cc.
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        data = await memento_oauth.list_workspaces(sub)
        if data is None:
            return json_response(request, {"connected": False, "orgs": [], "shared": [], "pinned": []})
        return json_response(request, {"connected": True, **data})

    async def pages(request: Request) -> JSONResponse:
        # Pages (documents) d'une KB — énumération paginée pour le browse dashboard.
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        ws = request.query_params.get("workspace") or None
        cursor = request.query_params.get("cursor") or None
        data = await memento_oauth.list_pages(sub, ws, cursor)
        if data is None:
            return json_response(request, {"connected": False, "items": []})
        return json_response(request, {"connected": True, **data})

    async def document(request: Request) -> JSONResponse:
        # Contenu d'une page (blocs ordonnés + document.url) par id ou path.
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        doc_id = request.query_params.get("id") or None
        path = request.query_params.get("path") or None
        if not doc_id and not path:
            return json_error(request, 400, "id_or_path_required")
        data = await memento_oauth.get_document(sub, doc_id=doc_id, path=path)
        if data is None:
            return json_response(request, {"connected": False})
        return json_response(request, {"connected": True, **data})

    return [
        Route("/api/memento/oauth/start", start, methods=["GET"]),
        Route("/api/memento/oauth/start", options_handler, methods=["OPTIONS"]),
        Route("/api/memento/oauth/callback", callback, methods=["GET"]),
        Route("/api/memento/oauth/status", status, methods=["GET"]),
        Route("/api/memento/oauth/status", options_handler, methods=["OPTIONS"]),
        Route("/api/memento/oauth", disconnect, methods=["DELETE"]),
        Route("/api/memento/oauth", options_handler, methods=["OPTIONS"]),
        Route("/api/memento/workspaces", workspaces, methods=["GET"]),
        Route("/api/memento/workspaces", options_handler, methods=["OPTIONS"]),
        Route("/api/memento/pages", pages, methods=["GET"]),
        Route("/api/memento/pages", options_handler, methods=["OPTIONS"]),
        Route("/api/memento/document", document, methods=["GET"]),
        Route("/api/memento/document", options_handler, methods=["OPTIONS"]),
    ]
