"""Adaptateur REST de la couche capacité (ADR 0009).

Boucle sur le registre et monte une Route Starlette par capacité ayant un
binding `rest`. Même séquence que l'adaptateur MCP : authenticate → input
(path_params + body) → autz → handler. L'`AuthzDenied` neutre est re-émis via
`json_error(request, status, code)` — **conserve l'enveloppe + les en-têtes
CORS** consommés par le dashboard.

Dépend du core (sens unique ADR 0004).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ._types import AuthzDenied, Capability, RawCtx

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def _make_handler(cap: Capability, binding, verifier, authenticate, json_response, json_error):
    async def _handler(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        data: dict = {}
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
                if isinstance(body, dict):
                    data.update(body)
            except Exception:
                pass
        # path params : mapping explicite placeholder->champ Input, sinon nom identique.
        for ph, value in request.path_params.items():
            field = (binding.path_map or {}).get(ph, ph)
            data[field] = value
        try:
            inp = cap.Input(**data)
        except ValidationError:
            return json_error(request, 400, "invalid_input")
        try:
            ctx = cap.authz(RawCtx(sub=sub), inp)
            result = cap.handler(ctx, inp)
        except AuthzDenied as d:
            return json_error(request, d.status, d.code)
        return json_response(request, result)
    return _handler


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
    capabilities: list[Capability],
) -> list[Route]:
    """Une Route (+ OPTIONS) par capacité REST. Liste vide si rien (canari)."""
    routes: list[Route] = []
    for cap in capabilities:
        for binding in cap.rest_bindings():
            h = _make_handler(cap, binding, verifier, authenticate, json_response, json_error)
            routes.append(Route(binding.path, h, methods=[binding.verb]))
            routes.append(Route(binding.path, options_handler, methods=["OPTIONS"]))
    return routes
