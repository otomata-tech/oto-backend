"""Routes REST de la MESSAGERIE HÉBERGÉE Unipile — connexion per-user + webhook.

⚠️ **Le palier PLATEFORME des connecteurs a quitté ce module le 2026-08-27** : le cran
d'activation (`/api/admin/connectors/activation`, ADR 0010 B4) et l'accès plateforme
(`/api/admin/connectors/{provider}/platform-access`, ADR 0044 §H) sont des CAPACITÉS
(`capabilities/platform_connectors.py`) — mêmes chemins, mêmes réponses, mais entrée ET
sortie déclarées. Les paliers org et équipe de la même famille l'étaient déjà
(`capabilities/connectors_activation.py`) ; c'est l'étage qui manquait. Le nom du
fichier est resté (il est le point d'accroche d'`api_routes.py`) ; ce qu'il porte, non.

Ce qui vit encore ici :

- `POST   /api/me/unipile/connect`   → URL de hosted-auth (nonce posé en `name`)
- `POST   /api/me/unipile/reconcile` → poll-and-bind explicite (le webhook v2 n'est pas livré)
- `GET    /api/me/unipile`           → statut per-user, avec self-heal opportuniste
- `DELETE /api/me/unipile`           → soft-déconnexion DANS l'org courante
- `POST   /api/unipile/webhook`      → **non authentifié** (Unipile appelle), gardé par le nonce
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import access, db, org_store

logger = logging.getLogger(__name__)


AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:


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
        # Corps partagé REST + MCP (`unipile_connect_start`, feedback #131) :
        # gates + nonce + hosted_auth_link vivent dans `unipile_connect`.
        from . import unipile_connect
        try:
            out = await unipile_connect.hosted_auth_url(
                sub, str(body.get("channel") or "linkedin"),
                force=bool(body.get("force")),
                # `premium` = 'recruiter' | 'sales_navigator' : produit LinkedIn à
                # ACTIVER à la connexion (sinon classic seul → 403 sur ces APIs).
                premium=(str(body["premium"]).strip().lower()
                         if body.get("premium") else None),
                # Face REST historique : oto-dashboard n'envoie pas `app` et
                # retombe donc sur sa propre destination, inchangée.
                app=(str(body["app"]) if body.get("app") else None))
        except unipile_connect.ConnectRefused as e:
            # 502 (échec amont) et 409 (doublon cross-org, #172) portent un message
            # actionnable → on le renvoie ; les autres exposent leur code machine.
            detail = e.message if e.status in (409, 502) else e.code
            return json_error(request, e.status, detail)
        # Adoption (binding-par-org) : le compte connecté ailleurs a été lié ICI sans
        # wizard → pas d'URL, le front rafraîchit ({adopted, account_name, channel}).
        if out.get("adopted"):
            return json_response(request, out)
        return json_response(request, {"url": out["url"]})

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
                # Filet : un pending émis AVANT le deploy B4 (BYO) porte org_id NULL
                # → org maison du sub (le binding doit toujours avoir une org).
                org_id = pend.get("org_id") or org_store.get_active_org(pend["sub"])
                db.set_unipile_account(pend["sub"], account_id, org_id=org_id,
                                       provider=pend.get("provider", "LINKEDIN"),
                                       platform_seat=bool(pend.get("platform_seat")))
                logger.info("unipile webhook: bound sub=%s account_id=%s org=%s",
                            pend["sub"], account_id, pend.get("org_id"))
            else:
                logger.warning("unipile webhook: nonce inconnu/expiré name=%s", name)
        elif status and status != "CREATION_SUCCESS":
            logger.info("unipile webhook: statut ignoré status=%s name=%s", status, name)
        return JSONResponse({"ok": True})

    async def unipile_status(request: Request) -> JSONResponse:
        """Statut de connexion Unipile per-user (pour le dashboard). **Self-heal** :
        le webhook hosted-auth v2 n'étant pas livré, on réconcilie (poll-and-bind)
        les comptes fraîchement connectés au chargement du statut — no-op sans
        pending (donc sans appel Unipile). Best-effort : jamais fatal pour le statut."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        from . import unipile_connect
        try:
            await asyncio.to_thread(unipile_connect.reconcile_pending, sub)
        except Exception:  # noqa: BLE001 — réconciliation opportuniste, jamais bloquante
            logger.warning("unipile status: reconcile best-effort échoué", exc_info=True)
        from .tools import unipile
        return json_response(request, unipile.status_for(sub))

    async def unipile_reconcile(request: Request) -> JSONResponse:
        """Poll-and-bind explicite (webhook v2 non livré) : lie le compte que `sub`
        vient de connecter. Le dashboard peut l'appeler au retour du hosted-auth
        (`?unipile=connected`). Idempotent."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        from . import unipile_connect
        out = await asyncio.to_thread(unipile_connect.reconcile_pending, sub)
        return json_response(request, out)

    async def unipile_disconnect(request: Request) -> JSONResponse:
        """SOFT-déconnecte le canal DANS CETTE ORG (ne supprime pas le compte chez
        Unipile ; la ligne survit comme preuve de propriété → rebind déterministe à la
        reconnexion). Par-org : le binding est un acte par org (modèle explicite) —
        et l'affichage ne montrant QUE les bindings de l'org courante, ce qu'on voit
        est ce qu'on déconnecte (plus de résurgence cross-org, ex-#221)."""
        sub, err = await authenticate(request, verifier)
        if err:
            return err
        provider = str(request.query_params.get("channel") or "linkedin").upper()
        db.clear_unipile_account(sub, access.current_org(sub), provider)
        return json_response(request, {"ok": True})



    return [
        # Les sièges de la clé plateforme unipile (inventaire + libération) sont des
        # CAPACITÉS depuis le 15/08 (`capabilities/unipile_seats.py`) : mêmes chemins,
        # dérivés — plus de route écrite ici.
        Route("/api/me/unipile/connect", unipile_connect, methods=["POST"]),
        Route("/api/me/unipile/connect", options_handler, methods=["OPTIONS"]),
        Route("/api/me/unipile/reconcile", unipile_reconcile, methods=["POST"]),
        Route("/api/me/unipile/reconcile", options_handler, methods=["OPTIONS"]),
        Route("/api/unipile/webhook", unipile_webhook, methods=["POST"]),
        Route("/api/me/unipile", unipile_status, methods=["GET"]),
        Route("/api/me/unipile", unipile_disconnect, methods=["DELETE"]),
        Route("/api/me/unipile", options_handler, methods=["OPTIONS"]),
    ]
