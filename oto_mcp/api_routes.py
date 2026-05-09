"""REST API consommée par le frontend oto.ninja (page de gestion de compte).

Endpoints :
- `GET    /api/me`                            → infos user + rôle + statut keys
- `POST   /api/settings/linkedin`             → enregistre cookie li_at + UA
- `DELETE /api/settings/linkedin`             → efface
- `POST   /api/settings/api-keys/{provider}`  → pose ta propre clé (provider in {serper, hunter, sirene})
- `DELETE /api/settings/api-keys/{provider}`  → efface
- `GET    /api/admin/users`                   → liste tous les users (admin only)
- `POST   /api/admin/users/{sub}/role`        → promeut/retrograde (admin only, body `{role}`)

Auth : Bearer JWT Logto, vérifié avec le même `JWTVerifier` que `/mcp` (le sub
du token = identifiant utilisateur côté DB). Le frontend obtient ce token via
le SDK `@logto/vue` après login OIDC.

CORS : limité aux origines oto.ninja (+ localhost en dev).
"""
from __future__ import annotations

import os
from typing import Iterable

import asyncio
import json

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import access, db, pairing


def _allowed_origins() -> list[str]:
    raw = os.environ.get("OTO_MCP_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://oto.ninja",
        "https://www.oto.ninja",
        "http://localhost:5173",
        "http://localhost:4173",
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


async def _authenticate(
    request: Request,
    verifier: JWTVerifier,
    *,
    allow_query_token: bool = False,
) -> tuple[str | None, JSONResponse | None]:
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif allow_query_token:
        # Fallback pour SSE via EventSource (qui n'autorise pas les headers).
        token = request.query_params.get("token")
    if not token:
        return None, _json_error(request, 401, "missing_bearer")
    access_token = await verifier.verify_token(token)
    if not access_token or not getattr(access_token, "claims", None):
        return None, _json_error(request, 401, "invalid_token")
    sub = access_token.claims.get("sub")
    if not sub:
        return None, _json_error(request, 401, "missing_sub")
    db.upsert_user(sub, email=access_token.claims.get("email"), name=access_token.claims.get("name"))
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
    from starlette.routing import Route

    async def options_handler(request: Request) -> Response:
        return Response(status_code=204, headers=_cors_headers(request.headers.get("origin")))

    async def me(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        user = db.get_user(sub) or {}
        status = access.status_for(sub)
        return _json(request, {
            "sub": sub,
            "email": user.get("email"),
            "name": user.get("name"),
            "role": status["role"],
            "linkedin": {
                "configured": bool(user.get("linkedin_cookie")),
                "set_at": user.get("linkedin_cookie_set_at"),
                "user_agent": user.get("linkedin_user_agent"),
            },
            "providers": status["providers"],
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

    async def api_key_save(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        if provider not in db.KEY_PROVIDERS:
            return _json_error(request, 404, "unknown_provider")
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(request, 400, "invalid_body")
        key = (body.get("key") or "").strip()
        if not key:
            return _json_error(request, 400, "empty_key")
        db.set_user_api_key(sub, provider, key)
        return _json(request, {"ok": True, "provider": provider})

    async def api_key_clear(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        provider = request.path_params["provider"]
        if provider not in db.KEY_PROVIDERS:
            return _json_error(request, 404, "unknown_provider")
        db.clear_user_api_key(sub, provider)
        return _json(request, {"ok": True, "provider": provider})

    async def admin_users(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        return _json(request, {"users": db.list_users()})

    async def admin_set_role(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        if access.get_user_role(sub) != access.ADMIN:
            return _json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        try:
            body = await request.json()
        except Exception:
            return _json_error(request, 400, "invalid_json")
        role = (body or {}).get("role")
        if role not in access.ROLES:
            return _json_error(request, 400, "invalid_role")
        if not db.get_user(target_sub):
            return _json_error(request, 404, "unknown_user")
        db.set_user_role(target_sub, role)
        return _json(request, {"ok": True, "sub": target_sub, "role": role})

    async def whatsapp_status(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        active = pairing.get_active_for_sub(sub)
        return _json(request, {
            "paired": pairing.is_paired(sub),
            "active_pairing": {
                "session_id": active.session_id,
                "status": active.status,
            } if active else None,
        })

    async def whatsapp_pair_start(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        loop = asyncio.get_running_loop()
        session = pairing.start(sub, loop)
        return _json(request, {"session_id": session.session_id, "status": session.status})

    async def whatsapp_pair_cancel(request: Request) -> JSONResponse:
        sub, err = await _authenticate(request, verifier)
        if err:
            return err
        active = pairing.get_active_for_sub(sub)
        if not active:
            return _json_error(request, 404, "no_active_pairing")
        active.cancel()
        return _json(request, {"ok": True})

    async def whatsapp_pair_stream(request: Request) -> Response:
        sub, err = await _authenticate(request, verifier, allow_query_token=True)
        if err:
            return err
        session_id = request.query_params.get("session_id", "")
        session = pairing.get_session(session_id)
        if not session or session.sub != sub:
            return _json_error(request, 404, "unknown_session")

        async def event_stream():
            # Initial hello so the client knows the stream is live.
            yield f": ok\ndata: {json.dumps({'type': 'connected', 'status': session.status})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(session.queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    # Keepalive comment.
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                **_cors_headers(request.headers.get("origin")),
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return [
        Route("/api/me", me, methods=["GET"]),
        Route("/api/me", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/linkedin", linkedin_save, methods=["POST"]),
        Route("/api/settings/linkedin", linkedin_clear, methods=["DELETE"]),
        Route("/api/settings/linkedin", options_handler, methods=["OPTIONS"]),
        Route("/api/settings/api-keys/{provider}", api_key_save, methods=["POST"]),
        Route("/api/settings/api-keys/{provider}", api_key_clear, methods=["DELETE"]),
        Route("/api/settings/api-keys/{provider}", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/status", whatsapp_status, methods=["GET"]),
        Route("/api/whatsapp/status", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/pair/start", whatsapp_pair_start, methods=["POST"]),
        Route("/api/whatsapp/pair/start", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/pair/cancel", whatsapp_pair_cancel, methods=["POST"]),
        Route("/api/whatsapp/pair/cancel", options_handler, methods=["OPTIONS"]),
        Route("/api/whatsapp/pair/stream", whatsapp_pair_stream, methods=["GET"]),
        Route("/api/whatsapp/pair/stream", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users", admin_users, methods=["GET"]),
        Route("/api/admin/users", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/role", admin_set_role, methods=["POST"]),
        Route("/api/admin/users/{sub}/role", options_handler, methods=["OPTIONS"]),
    ]
