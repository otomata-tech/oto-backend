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

from . import access, billing, connector_activation, db, org_store, providers

logger = logging.getLogger(__name__)


def _unipile_default_limit() -> int:
    """Plafond par défaut de comptes Unipile par org (anti-dérapage coût) si l'org
    n'en définit pas un propre. 0 = pas de plafond."""
    try:
        return int(os.environ.get("OTO_MCP_UNIPILE_DEFAULT_LIMIT", "5"))
    except ValueError:
        return 5

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
        if not access.is_platform_operator(sub):
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
        try:
            body = await request.json()
        except Exception:
            body = {}
        provider = str(body.get("channel") or "linkedin").upper()
        if provider not in ("LINKEDIN", "WHATSAPP", "TELEGRAM", "INSTAGRAM", "MESSENGER", "TWITTER"):
            return json_error(request, 400, "invalid_channel")
        api_key = access.unipile_api_key_for(sub)
        if not api_key:
            return json_error(request, 404, "unipile_not_configured")
        # org « porteur » du compte = org actif, SAUF si l'user a sa propre clé (BYO)
        # → c'est son abonnement, pas celui d'un org (pas de plafond org).
        byo = db.get_user_api_key(sub, "unipile") is not None
        org_id = None if byo else org_store.get_active_org(sub)
        # Gate ABONNEMENT (option LinkedIn payée €15/mois/siège) : on n'autorise la
        # connexion que si l'org a un abonnement actif. BYO (clé perso) = l'user paie
        # Unipile en direct → pas de gate. Le dashboard, sur ce 402, lance le checkout.
        if org_id is not None and not billing.has_active_unipile_subscription(org_id):
            return json_error(request, 402, "unipile_subscription_required")
        # Plafond anti-dérapage : chaque compte connecté coûte ~5 €/mois sur la
        # facture Unipile de l'org. On bloque une NOUVELLE connexion au-delà du
        # plafond (un user qui a déjà un compte peut reconnecter = remplacement).
        if org_id is not None and db.get_unipile_account(sub, provider) is None:
            limit = db.get_org_unipile_limit(org_id)
            if limit is None:
                limit = _unipile_default_limit()
            if limit and db.count_unipile_accounts_for_org(org_id) >= limit:
                logger.info("unipile cap hit org=%s limit=%s", org_id, limit)
                return json_error(request, 429, "unipile_account_limit_reached")
        from oto.tools.unipile import UnipileClient
        client = UnipileClient(api_key=api_key)
        public = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
        dash = os.environ.get("OTO_DASHBOARD_URL", "https://dashboard.oto.ninja").rstrip("/")
        nonce = secrets.token_urlsafe(24)
        db.create_unipile_pending(nonce, sub, org_id, provider)
        ch = provider.lower()
        try:
            url = await asyncio.to_thread(
                client.hosted_auth_link,
                name=nonce,
                providers=[provider],
                notify_url=f"{public}/api/unipile/webhook",
                success_redirect_url=f"{dash}/console/connections?unipile=connected&channel={ch}",
                failure_redirect_url=f"{dash}/console/connections?unipile=failed&channel={ch}",
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
            pend = db.resolve_unipile_pending(name)
            if pend:
                db.set_unipile_account(pend["sub"], account_id, org_id=pend.get("org_id"),
                                       provider=pend.get("provider", "LINKEDIN"))
                logger.info("unipile webhook: bound sub=%s account_id=%s org=%s",
                            pend["sub"], account_id, pend.get("org_id"))
                # Aligne la quantité de l'abonnement Stripe sur le nb de sièges.
                if pend.get("org_id"):
                    await asyncio.to_thread(billing.sync_unipile_seats, pend["org_id"])
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
        accts = {a["provider"]: a for a in db.list_unipile_accounts(sub)}
        # État de l'abonnement de l'org active (gate l'étape « connecter »). BYO
        # (clé perso) → considéré subscribed (l'user paie Unipile en direct).
        org_id = org_store.get_active_org(sub)
        byo = db.get_user_api_key(sub, "unipile") is not None
        subscribed = byo or (org_id is not None and billing.has_active_unipile_subscription(org_id))

        def _ch(p: str) -> dict:
            a = accts.get(p)
            return {
                "connected": a is not None,
                "account_id": a["account_id"] if a else None,
                "connected_at": str(a["connected_at"]) if a else None,
            }
        return json_response(request, {
            "subscribed": subscribed,
            "channels": {
                "linkedin": _ch("LINKEDIN"), "whatsapp": _ch("WHATSAPP"),
                "telegram": _ch("TELEGRAM"), "instagram": _ch("INSTAGRAM"),
                "messenger": _ch("MESSENGER"), "twitter": _ch("TWITTER"),
            },
        })

    async def unipile_subscribe(request: Request) -> JSONResponse:
        """Démarre l'abonnement « option LinkedIn » (€15/mois/siège) de l'org active.
        Renvoie `{checkout_url}` (Stripe `mode=subscription`). Quantité initiale =
        nb de comptes déjà connectés (≥1). Le webhook marque l'org active au paiement."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return json_error(request, 400, "no_active_org")
        qty = max(1, db.count_unipile_accounts_for_org(org_id))
        try:
            res = await asyncio.to_thread(
                billing.create_unipile_subscription_checkout, org_id, sub, qty)
        except Exception as e:
            return json_error(request, 502, f"unipile_subscribe_failed: {e}")
        return json_response(request, res)

    async def unipile_disconnect(request: Request) -> JSONResponse:
        """Oublie l'association compte LinkedIn ↔ user (ne supprime pas le compte
        chez Unipile, juste le mapping côté oto). Réaligne les sièges de l'abonnement."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        provider = str(request.query_params.get("channel") or "linkedin").upper()
        had = db.get_unipile_account(sub, provider) is not None
        db.clear_unipile_account(sub, provider)
        org_id = org_store.get_active_org(sub)
        if had and org_id is not None:
            await asyncio.to_thread(billing.sync_unipile_seats, org_id)
        return json_response(request, {"ok": True})

    return [
        Route("/api/admin/connectors/activation", list_activation, methods=["GET"]),
        Route("/api/admin/connectors/activation", set_activation, methods=["POST"]),
        Route("/api/admin/connectors/activation", clear_override, methods=["DELETE"]),
        Route("/api/admin/connectors/activation", options_handler, methods=["OPTIONS"]),
        Route("/api/me/unipile/connect", unipile_connect, methods=["POST"]),
        Route("/api/me/unipile/connect", options_handler, methods=["OPTIONS"]),
        Route("/api/me/unipile/subscribe", unipile_subscribe, methods=["POST"]),
        Route("/api/me/unipile/subscribe", options_handler, methods=["OPTIONS"]),
        Route("/api/unipile/webhook", unipile_webhook, methods=["POST"]),
        Route("/api/me/unipile", unipile_status, methods=["GET"]),
        Route("/api/me/unipile", unipile_disconnect, methods=["DELETE"]),
        Route("/api/me/unipile", options_handler, methods=["OPTIONS"]),
    ]
