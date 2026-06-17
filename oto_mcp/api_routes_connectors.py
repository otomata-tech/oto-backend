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
import json
import logging
import os
import secrets
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import access, connector_activation, db, providers

logger = logging.getLogger(__name__)

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
        """Hosted-auth Unipile (B2) : génère l'URL où l'user connecte SON LinkedIn
        sous l'abonnement partagé (clé de son org). On pose un **nonce** aléatoire
        comme `name` (le `name` ne revient pas dans /accounts → corrélation via le
        webhook `notify_url` qui, lui, l'échoit). Per-user (pas admin)."""
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
        nonce = secrets.token_urlsafe(24)
        db.create_unipile_pending(nonce, sub)
        try:
            url = await asyncio.to_thread(
                client.hosted_auth_link,
                name=nonce,
                notify_url=f"{public}/api/unipile/webhook",
                success_redirect_url=f"{dash}/console/connections?unipile=connected",
                failure_redirect_url=f"{dash}/console/connections?unipile=failed",
            )
        except Exception as e:
            return json_error(request, 502, f"unipile_link_failed: {e}")
        if not url:
            return json_error(request, 502, "unipile_link_empty")
        return json_response(request, {"url": url})

    async def unipile_webhook(request: Request) -> JSONResponse:
        """Notification Unipile au succès du hosted-auth (B3). **NON authentifié**
        (Unipile l'appelle, server-to-server) → sécurisé par le **nonce** : on ne
        lie le compte que si `name` est un nonce VIVANT qu'on a nous-mêmes posé
        (non devinable, court). Logue le payload brut pour instrumenter le format
        réel. Toujours 200 (ack ; un échec ne doit pas faire rejouer Unipile en
        boucle)."""
        raw = await request.body()
        logger.info("unipile webhook raw=%s", raw[:2000])
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            return JSONResponse({"ok": True})
        # Format réel confirmé (instrumenté 2026-06-18) :
        # {status:"CREATION_SUCCESS", account_id, name:<nonce>, account_type}.
        # On ne lie QUE sur un succès de création — un événement d'échec/autre ne
        # doit pas mapper un account_id. Le nonce (consommé au 1er resolve) protège
        # déjà du double-binding.
        status = body.get("status")
        name = body.get("name")
        account_id = body.get("account_id") or body.get("accountId") or body.get("id")
        if status == "CREATION_SUCCESS" and name and account_id:
            sub = db.resolve_unipile_pending(name)
            if sub:
                db.set_unipile_account(sub, account_id)
                logger.info("unipile webhook: bound sub=%s account_id=%s", sub, account_id)
            else:
                logger.warning("unipile webhook: nonce inconnu/expiré name=%s", name)
        elif status and status != "CREATION_SUCCESS":
            logger.info("unipile webhook: statut ignoré status=%s name=%s", status, name)
        return JSONResponse({"ok": True})

    async def unipile_status(request: Request) -> JSONResponse:
        """Statut de connexion Unipile per-user (pour le dashboard)."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        acc = db.get_unipile_account(sub)
        return json_response(request, {
            "connected": acc is not None,
            "account_id": acc["account_id"] if acc else None,
            "connected_at": str(acc["connected_at"]) if acc else None,
        })

    async def unipile_disconnect(request: Request) -> JSONResponse:
        """Oublie l'association compte LinkedIn ↔ user (ne supprime pas le compte
        chez Unipile, juste le mapping côté oto)."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        db.clear_unipile_account(sub)
        return json_response(request, {"ok": True})

    return [
        Route("/api/admin/connectors/activation", list_activation, methods=["GET"]),
        Route("/api/admin/connectors/activation", set_activation, methods=["POST"]),
        Route("/api/admin/connectors/activation", clear_override, methods=["DELETE"]),
        Route("/api/admin/connectors/activation", options_handler, methods=["OPTIONS"]),
        Route("/api/me/unipile/connect", unipile_connect, methods=["POST"]),
        Route("/api/me/unipile/connect", options_handler, methods=["OPTIONS"]),
        Route("/api/unipile/webhook", unipile_webhook, methods=["POST"]),
        Route("/api/me/unipile", unipile_status, methods=["GET"]),
        Route("/api/me/unipile", unipile_disconnect, methods=["DELETE"]),
        Route("/api/me/unipile", options_handler, methods=["OPTIONS"]),
    ]
