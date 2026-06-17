"""Routes REST `/api/admin/connectors/activation` — gouvernance du cran
d'activation des connecteurs (ADR 0010, B4). Admin only.

Le code DÉCLARE les connecteurs (registre `providers.py`) ; la DB décide
lesquels sont EXPOSÉS (`connector_activation`). Cet endpoint est la surface qui
permet à un admin de basculer le master global (et un override par org) sans SQL.

- `GET    /api/admin/connectors/activation`  → tout le registre × état (global + overrides)
- `POST   /api/admin/connectors/activation`  → {connector, enabled, org_id?} pose l'activation
- `DELETE /api/admin/connectors/activation?connector=&org_id=` → supprime un override d'org

⚠️ Le gate est au CHARGEMENT (register_all, au boot) : (dés)activer le master
global prend effet au prochain redémarrage du serveur (`restart_required` dans la
réponse POST). L'override d'org est posé en DB de la même façon.
"""
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import access, connector_activation, providers

AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

    async def _admin(request: Request) -> tuple[str | None, JSONResponse | None]:
        sub, err = await authenticate(request, verifier)
        if err:
            return None, err
        if access.get_user_role(sub) != access.ADMIN:
            return None, json_error(request, 403, "forbidden")
        return sub, None

    async def list_activation(request: Request) -> JSONResponse:
        """Tout le registre × son état d'activation (global + overrides d'org).

        `enabled` : True/False (master global posé) ou null (jamais posé → OFF,
        deny-by-default). L'admin voit TOUT le registre, même les connecteurs OFF
        (c'est sa surface pour les activer)."""
        sub, err = await _admin(request)
        if err:
            return err
        glob: dict[str, bool] = {}
        overrides: dict[str, list] = {}
        for r in connector_activation.list_activations():
            if r["org_id"] is None:
                glob[r["connector"]] = bool(r["enabled"])
            else:
                overrides.setdefault(r["connector"], []).append(
                    {"org_id": r["org_id"], "enabled": bool(r["enabled"])}
                )
        out = [
            {
                "connector": name,
                "label": c.label,
                "help": c.help,
                "namespaces": list(c.namespaces),
                "enabled": glob.get(name),  # None = jamais posé = OFF
                "overrides": overrides.get(name, []),
            }
            for name, c in providers.REGISTRY.items()
        ]
        return json_response(request, {"connectors": out})

    async def set_activation(request: Request) -> JSONResponse:
        """Pose l'activation : master global si `org_id` absent, sinon override d'org.
        Body {connector, enabled: bool, org_id?: int}."""
        sub, err = await _admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return json_error(request, 400, "invalid_json")
        connector = body.get("connector")
        enabled = body.get("enabled")
        org_id = body.get("org_id")
        if connector not in providers.REGISTRY:
            return json_error(request, 400, "unknown_connector")
        if not isinstance(enabled, bool):
            return json_error(request, 400, "enabled_must_be_bool")
        if org_id is not None and not isinstance(org_id, int):
            return json_error(request, 400, "org_id_must_be_int")
        connector_activation.set_activation(connector, enabled, org_id=org_id, set_by=sub)
        # Le chargement des tools est résolu au boot → un changement de master
        # global ne prend effet qu'au prochain redémarrage.
        return json_response(request, {
            "ok": True,
            "connector": connector,
            "enabled": enabled,
            "org_id": org_id,
            "restart_required": org_id is None,
        })

    async def clear_override(request: Request) -> JSONResponse:
        """Supprime un override d'org (le connecteur retombe sur le master global).
        Query `?connector=&org_id=`."""
        sub, err = await _admin(request)
        if err:
            return err
        connector = request.query_params.get("connector")
        org_id_raw = request.query_params.get("org_id")
        if not connector or not org_id_raw:
            return json_error(request, 400, "connector_and_org_id_required")
        try:
            org_id = int(org_id_raw)
        except ValueError:
            return json_error(request, 400, "org_id_must_be_int")
        connector_activation.clear_activation(connector, org_id)
        return json_response(request, {"ok": True, "connector": connector, "org_id": org_id})

    async def unipile_connect(request: Request) -> JSONResponse:
        """Hosted-auth Unipile (B2) : génère l'URL où l'user connecte son LinkedIn.

        Per-user (pas admin). Utilise le credential Unipile résolu pour lui (sa clé
        BYO, sinon l'abonnement de son org active). Unipile gère 2FA/checkpoints
        sur sa page hébergée puis POST l'`account_id` sur le webhook
        `/api/unipile/webhook` (B3) au succès."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        api_key = access.unipile_api_key_for(sub)
        if not api_key:
            return json_error(request, 404, "unipile_not_configured")
        from oto.tools.unipile import UnipileClient
        client = UnipileClient(api_key=api_key)
        public = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
        dash = os.environ.get("OTO_DASHBOARD_URL", "https://dashboard.oto.ninja").rstrip("/")
        try:
            url = await asyncio.to_thread(
                client.hosted_auth_link,
                notify_url=f"{public}/api/unipile/webhook",
                name=sub,
                success_redirect_url=f"{dash}/console/connections?unipile=connected",
                failure_redirect_url=f"{dash}/console/connections?unipile=failed",
            )
        except Exception as e:
            return json_error(request, 502, f"unipile_link_failed: {e}")
        if not url:
            return json_error(request, 502, "unipile_link_empty")
        return json_response(request, {"url": url})

    return [
        Route("/api/admin/connectors/activation", list_activation, methods=["GET"]),
        Route("/api/admin/connectors/activation", set_activation, methods=["POST"]),
        Route("/api/admin/connectors/activation", clear_override, methods=["DELETE"]),
        Route("/api/admin/connectors/activation", options_handler, methods=["OPTIONS"]),
        Route("/api/me/unipile/connect", unipile_connect, methods=["POST"]),
        Route("/api/me/unipile/connect", options_handler, methods=["OPTIONS"]),
    ]
