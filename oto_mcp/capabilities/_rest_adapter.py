"""Adaptateur REST de la couche capacité (ADR 0009).

Boucle sur le registre et monte une Route Starlette par capacité ayant un
binding `rest`. Même séquence que l'adaptateur MCP : authenticate → input
(path_params + body) → autz → handler. L'`AuthzDenied` neutre est re-émis via
`json_error(request, status, code)` — **conserve l'enveloppe + les en-têtes
CORS** consommés par le dashboard.

Dépend du core (sens unique ADR 0004).
"""
from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Optional

from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import roles, session_org
from ._types import AuthzDenied, Capability, RawCtx

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def _parse_view_org(request: Request) -> Optional[int]:
    """Org de consultation demandée (header `X-Oto-Org`, view-as dashboard, ADR 0023).
    None = pas de header ; 0 = profil perso ; >0 = id d'org. Header invalide → None
    (repli silencieux sur l'org maison, jamais d'erreur dure sur un en-tête mal formé)."""
    raw = request.headers.get("x-oto-org")
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("", "0", "perso", "personal"):
        return 0
    try:
        n = int(v)
        return n if n > 0 else 0
    except ValueError:
        return None


def _make_handler(cap: Capability, binding, verifier, authenticate, json_response, json_error):
    async def _handler(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        data: dict = {}
        # Query string (filtres des GET/DELETE sans body : `?query=…&limit=…`).
        # Valeurs str → pydantic coerce vers le type du champ Input. Priorité la
        # plus basse (body puis path params écrasent).
        if request.query_params:
            data.update(dict(request.query_params))
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
        # View-as (ADR 0023) : consulter une AUTRE org sans muter l'identité — header
        # `X-Oto-Org`. Validé par APPARTENANCE ici (anti-IDOR : ne jamais faire
        # confiance à l'en-tête), posé en contextvar lu par `access.current_org`, puis
        # reset. Une org=0 (perso) ne nécessite pas de check (profil global du user).
        view = _parse_view_org(request)
        if view and not roles.is_org_member(sub, view):
            return json_error(request, 403, "forbidden")
        token = session_org.set_view_org(view) if view is not None else None
        try:
            ctx = cap.authz(RawCtx(sub=sub), inp)
            result = cap.handler(ctx, inp)
            if inspect.isawaitable(result):           # handler async (ex. doctrine + manifeste)
                result = await result
        except AuthzDenied as d:
            return json_error(request, d.status, d.code)
        finally:
            if token is not None:
                session_org.reset_view_org(token)
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
