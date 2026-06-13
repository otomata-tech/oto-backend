"""Routes REST `/api/scout/*` — surface du harnais prospection (ADR 0008).

Adaptateur REST par-dessus `factgraph.prospection` (le service org-scopé).
Consommé par le cockpit oto-dashboard (frontend sans serveur propre).

- `GET  /api/scout/queue`                      → file priorisée (qualified libres, heat→fit)
- `POST /api/scout/claim-next`                 → claim atomique du prochain prospect
- `GET  /api/scout/prospects/{id}`             → fiche (entreprise + contacts + actions)
- `POST /api/scout/prospects`                  → créer un prospect (entreprise)
- `POST /api/scout/prospects/{id}/contacts`    → ajouter un contact
- `POST /api/scout/prospects/{id}/actions`     → enregistrer une action (fait avancer le statut)

Scope : le workspace prospection de l'**org active** du token. Auth = même
`_authenticate` que le reste (Bearer Logto JWT ou API token `oto_*`).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import org_store
from .factgraph import prospection
from .factgraph.schemas import SchemaError

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    async def _auth_org(request: Request) -> tuple[str | None, int | None, JSONResponse | None]:
        """(sub, org_id, err) — exige un token valide ET une org active."""
        sub, err = await authenticate(request, verifier)
        if err:
            return None, None, err
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return sub, None, json_error(request, 400, "no_active_org")
        return sub, org_id, None

    def _fact_id(request: Request) -> int | None:
        raw = request.path_params.get("id")
        return int(raw) if raw and raw.isdigit() else None

    async def queue(request: Request) -> JSONResponse:
        sub, org_id, err = await _auth_org(request)
        if err:
            return err
        limit = min(int(request.query_params.get("limit", 50) or 50), 500)
        items = prospection.queue(org_id, limit)
        return json_response(request, {"items": items, "count": len(items)})

    async def claim_next(request: Request) -> JSONResponse:
        sub, org_id, err = await _auth_org(request)
        if err:
            return err
        picked = prospection.claim_next(org_id, who=sub)
        return json_response(request, {"prospect": picked})

    async def prospect_detail(request: Request) -> JSONResponse:
        sub, org_id, err = await _auth_org(request)
        if err:
            return err
        fid = _fact_id(request)
        if fid is None:
            return json_error(request, 400, "invalid_id")
        try:
            return json_response(request, prospection.get_detail(org_id, fid))
        except (KeyError, ValueError):
            return json_error(request, 404, "not_found")

    async def create_prospect(request: Request) -> JSONResponse:
        sub, org_id, err = await _auth_org(request)
        if err:
            return err
        body = await request.json()
        try:
            fid = prospection.add_prospect(
                org_id, siren=body["siren"], nom=body["nom"],
                bp_an=body.get("bp_an"), idcc=body.get("idcc"), created_by=sub or "system",
            )
        except (KeyError, SchemaError):
            return json_error(request, 400, "invalid_prospect")
        return json_response(request, prospection.get_detail(fid))

    async def add_contact(request: Request) -> JSONResponse:
        sub, org_id, err = await _auth_org(request)
        if err:
            return err
        fid = _fact_id(request)
        if fid is None:
            return json_error(request, 400, "invalid_id")
        body = await request.json()
        try:
            prospection.add_contact(org_id, fid, nom=body.get("nom"), tel=body.get("tel"),
                                    linkedin=body.get("linkedin"), created_by=sub or "system")
        except KeyError:
            return json_error(request, 404, "not_found")
        except (SchemaError, ValueError):
            return json_error(request, 400, "invalid_contact")
        return json_response(request, prospection.get_detail(org_id, fid))

    async def add_action(request: Request) -> JSONResponse:
        sub, org_id, err = await _auth_org(request)
        if err:
            return err
        fid = _fact_id(request)
        if fid is None:
            return json_error(request, 400, "invalid_id")
        body = await request.json()
        try:
            detail = prospection.record_action(
                org_id, fid, canal=body.get("canal"), outcome=body.get("outcome"),
                note=body.get("note"), created_by=sub or "system",
            )
        except KeyError:
            return json_error(request, 404, "not_found")
        except (SchemaError, ValueError):
            return json_error(request, 400, "invalid_action")
        return json_response(request, detail)

    return [
        Route("/api/scout/queue", queue, methods=["GET"]),
        Route("/api/scout/queue", options_handler, methods=["OPTIONS"]),
        Route("/api/scout/claim-next", claim_next, methods=["POST"]),
        Route("/api/scout/claim-next", options_handler, methods=["OPTIONS"]),
        Route("/api/scout/prospects", create_prospect, methods=["POST"]),
        Route("/api/scout/prospects", options_handler, methods=["OPTIONS"]),
        Route("/api/scout/prospects/{id}", prospect_detail, methods=["GET"]),
        Route("/api/scout/prospects/{id}", options_handler, methods=["OPTIONS"]),
        Route("/api/scout/prospects/{id}/contacts", add_contact, methods=["POST"]),
        Route("/api/scout/prospects/{id}/contacts", options_handler, methods=["OPTIONS"]),
        Route("/api/scout/prospects/{id}/actions", add_action, methods=["POST"]),
        Route("/api/scout/prospects/{id}/actions", options_handler, methods=["OPTIONS"]),
    ]
