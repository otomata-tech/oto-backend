"""LA route qui reste écrite à la main du domaine connecteurs : le WEBHOOK Unipile.

⚠️ **Tout le reste a migré en capacités le 2026-08-27** — mêmes chemins, mêmes réponses,
entrée ET sortie déclarées :
- le palier PLATEFORME (cran d'activation ADR 0010 B4 + accès plateforme ADR 0044 §H)
  → `capabilities/platform_connectors.py` ;
- la messagerie hébergée côté MEMBRE (`/api/me/unipile*`)
  → `capabilities/unipile_me.py`.

Le nom du fichier est resté (il est le point d'accroche d'`api_routes.py`) ; ce qu'il
porte, non.

**Pourquoi le webhook ne migre pas, et ne migrera pas.** Unipile l'appelle
server-to-server, **sans en-tête d'auth** — or `_rest_adapter` authentifie TOUJOURS : un
anonyme ne peut pas y passer, par construction. Il est classé par NATURE, comme les
callbacks OAuth et les autres webhooks.

Sa sécurité tient au **nonce** : on ne lie un compte que si `name` est un jeton VIVANT
que nous avons nous-mêmes posé (non devinable, court, consommé au premier resolve). Il
répond **toujours 200** — un échec ne doit pas faire rejouer Unipile en boucle.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db, org_store

logger = logging.getLogger(__name__)


AuthFn = Callable[..., Awaitable[tuple[str | None, JSONResponse | None]]]


def make_routes(
    verifier: JWTVerifier,
    authenticate: AuthFn,
    json_response: Callable[..., JSONResponse],
    json_error: Callable[..., JSONResponse],
    options_handler: Callable[[Request], Awaitable[Response]],
) -> list[Route]:

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




    return [
        # Les sièges de la clé plateforme unipile (inventaire + libération) sont des
        # CAPACITÉS depuis le 15/08 (`capabilities/unipile_seats.py`) : mêmes chemins,
        # dérivés — plus de route écrite ici.
        Route("/api/unipile/webhook", unipile_webhook, methods=["POST"]),
    ]
