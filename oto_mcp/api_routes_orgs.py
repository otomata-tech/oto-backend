"""Routes REST `/api/admin/namespace-grants*` — grants de namespace **per-user**
(deny-by-default sur les namespaces gouvernés), consommés par le SPA admin.

> Tout le **domaine orgs** (use_org, membres, secrets, create, entitlements,
> lectures list/get) a migré vers la **couche capacité** (ADR 0009, barreaux
> 1→2d) : `oto_mcp/capabilities/orgs*.py`, monté par les adaptateurs MCP/REST.
> Ce module ne porte plus que les grants de namespace per-user (≠ org).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import access, db
from .tool_visibility import ADMIN_GRANT_ONLY_NAMESPACES

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    def _is_platform_admin(sub: str) -> bool:
        # Routes admin sur membres/secrets d'orgs tierces = escalade en masse →
        # réservé au super_admin (pas à l'admin opérationnel).
        return access.is_super_admin(sub)

    async def admin_namespace_grants_list(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        grants = []
        for g in db.list_namespace_grants():
            u = db.get_user(g["sub"]) or {}
            grants.append({
                "sub": g["sub"],
                "email": u.get("email"),
                "name": u.get("name"),
                "namespace": g["namespace"],
                "granted_at": g["granted_at"],
            })
        return json_response(request, {"grants": grants})

    async def admin_namespace_grant(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        namespace = request.path_params["namespace"]
        if namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
            return json_error(request, 400, "namespace_not_controlled")
        if not db.get_user(target_sub):
            return json_error(request, 404, "unknown_user")
        db.grant_namespace(target_sub, namespace, granted_by=sub)
        return json_response(request, {"ok": True, "sub": target_sub, "namespace": namespace})

    async def admin_namespace_revoke(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        if not _is_platform_admin(sub):
            return json_error(request, 403, "forbidden")
        target_sub = request.path_params["sub"]
        namespace = request.path_params["namespace"]
        existed = db.revoke_namespace(target_sub, namespace)
        return json_response(request, {"ok": True, "sub": target_sub,
                                       "namespace": namespace, "existed": existed})

    return [
        Route("/api/admin/namespace-grants", admin_namespace_grants_list, methods=["GET"]),
        Route("/api/admin/namespace-grants", options_handler, methods=["OPTIONS"]),
        Route("/api/admin/users/{sub}/namespace-grants/{namespace}", admin_namespace_grant, methods=["POST"]),
        Route("/api/admin/users/{sub}/namespace-grants/{namespace}", admin_namespace_revoke, methods=["DELETE"]),
        Route("/api/admin/users/{sub}/namespace-grants/{namespace}", options_handler, methods=["OPTIONS"]),
    ]
