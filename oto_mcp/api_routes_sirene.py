"""Routes REST `/api/sirene/*` — consommé par oto-cli (HTTP client) et autres
scripts qui veulent du batch enrichment sans gérer un parquet local.

Backend = `sirene_duckdb` (DuckDB sur le parquet INSEE).

- `GET /api/sirene/siege?siren=`                 → siège (1 dict ou null)
- `GET /api/sirene/etablissements?siren=`        → tous établissements (list)
- `GET /api/sirene/siret?siret=`                 → 1 établissement
- `GET /api/sirene/search?naf=&code_commune=...` → paginé
- `GET /api/sirene/info`                         → métadonnées parquet (size, mtime, count)

Auth : Bearer Logto JWT ou API token `oto_*` (même `_authenticate` que le reste).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import sirene_duckdb


AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def _qp(request: Request, name: str) -> str | None:
    v = request.query_params.get(name)
    return v.strip() if v else None


def _qp_int(request: Request, name: str, default: int) -> int:
    v = request.query_params.get(name)
    try:
        return int(v) if v else default
    except ValueError:
        return default


def _qp_bool(request: Request, name: str, default: bool) -> bool:
    v = request.query_params.get(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    async def siege(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        siren = _qp(request, "siren")
        if not siren or not siren.isdigit() or len(siren) != 9:
            return json_error(request, 400, "invalid_siren")
        return json_response(request, {"siege": sirene_duckdb.lookup_siege(siren)})

    async def etablissements(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        siren = _qp(request, "siren")
        if not siren or not siren.isdigit() or len(siren) != 9:
            return json_error(request, 400, "invalid_siren")
        active_only = _qp_bool(request, "active_only", True)
        items = sirene_duckdb.list_establishments(siren, active_only=active_only)
        return json_response(request, {"items": items, "count": len(items)})

    async def siret(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        s = _qp(request, "siret")
        if not s or not s.isdigit() or len(s) != 14:
            return json_error(request, 400, "invalid_siret")
        return json_response(request, {"etablissement": sirene_duckdb.lookup_siret(s)})

    async def search(request: Request) -> JSONResponse:
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        items = sirene_duckdb.search(
            naf=_qp(request, "naf"),
            code_commune=_qp(request, "code_commune"),
            code_postal=_qp(request, "code_postal"),
            denomination=_qp(request, "denomination"),
            enseigne=_qp(request, "enseigne"),
            active_only=_qp_bool(request, "active_only", True),
            sieges_only=_qp_bool(request, "sieges_only", False),
            limit=_qp_int(request, "limit", 100),
            offset=_qp_int(request, "offset", 0),
        )
        return json_response(request, {
            "items": items,
            "count": len(items),
            "limit": _qp_int(request, "limit", 100),
            "offset": _qp_int(request, "offset", 0),
        })

    async def info(request: Request) -> JSONResponse:
        # Public-ish — utile pour healthcheck depuis n'importe quel client.
        # Auth quand même pour éviter de divulguer la taille.
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        return json_response(request, sirene_duckdb.parquet_info())

    return [
        Route("/api/sirene/siege", siege, methods=["GET"]),
        Route("/api/sirene/siege", options_handler, methods=["OPTIONS"]),
        Route("/api/sirene/etablissements", etablissements, methods=["GET"]),
        Route("/api/sirene/etablissements", options_handler, methods=["OPTIONS"]),
        Route("/api/sirene/siret", siret, methods=["GET"]),
        Route("/api/sirene/siret", options_handler, methods=["OPTIONS"]),
        Route("/api/sirene/search", search, methods=["GET"]),
        Route("/api/sirene/search", options_handler, methods=["OPTIONS"]),
        Route("/api/sirene/info", info, methods=["GET"]),
        Route("/api/sirene/info", options_handler, methods=["OPTIONS"]),
    ]
