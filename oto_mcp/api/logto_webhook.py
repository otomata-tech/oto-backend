"""La route que Logto appelle quand un mot de passe change — `POST /api/logto/webhook`.

Écrite à la main, hors de la couche capacité, pour la raison du webhook Mollie et de
celui des agents : la preuve est un HMAC du CORPS BRUT (`logto-signature-sha-256`), que
l'adaptateur de capacité — qui parse et refuse champ par champ — ne laisse pas lire.
Aucun JWT : la signature EST l'authentification.

Elle adapte, `auth.revocation_mot_de_passe.traiter` décide. Tout passe par
`run_in_threadpool` (base et Management API sont synchrones, la boucle est unique).

Réponses :
  200 `{ignored: true}` — livraison signée qui ne dit pas un mot de passe changé ;
      `{ignored: false, revoked_tokens, closed_sessions, revoked_grants}` sinon
  400 `invalid_event` (corps illisible ou pas un objet) / `stale_event` (`createdAt`
      hors fenêtre) — jugés APRÈS la signature
  401 `invalid_signature` — signature absente ou fausse
  413 `payload_too_large`
  502 `logto_sessions_not_closed` — jetons coupés, Logto pas entièrement fermé : Logto
      retente, et tout le geste est idempotent
  503 `logto_webhook_not_configured` — l'instance ne déclare pas de clé de signature
  500 `logto_webhook_failed` — panne de notre côté (base…), journalisée
"""
from __future__ import annotations

import json
import logging

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..auth import annuaire, revocation_mot_de_passe as revocation
from .hooks import _lire_borne

logger = logging.getLogger(__name__)

PATH = "/api/logto/webhook"


def _refus(statut: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "detail": message}, status_code=statut)


async def logto_webhook(request: Request) -> JSONResponse:
    brut = await _lire_borne(request)
    if brut is None:
        return _refus(413, "payload_too_large", "Body above the webhook limit.")
    try:
        evt = json.loads(brut)
    except ValueError:
        # Jugé APRÈS la signature (`traiter`) : un inconnu n'apprend rien de son corps.
        evt = None
    try:
        rendu = await run_in_threadpool(
            revocation.traiter, brut, evt,
            request.headers.get(revocation.ENTETE_SIGNATURE))
    except revocation.Refus as r:
        return _refus(r.statut, r.code, r.message)
    except annuaire.FermetureInachevee:
        logger.exception("webhook Logto : sessions non fermées — Logto retentera")
        return _refus(502, "logto_sessions_not_closed",
                      "API tokens were revoked; closing the Logto sessions failed. "
                      "Retry the delivery.")
    except Exception:  # noqa: BLE001 — journalisé entier ; Logto retente un 500
        logger.exception("webhook Logto : livraison en échec de notre côté")
        return _refus(500, "logto_webhook_failed",
                      "The delivery failed on our side. Retry.")
    return JSONResponse(rendu, status_code=200)


def make_routes() -> list[Route]:
    """Appelée de serveur à serveur : pas de pré-vol CORS."""
    return [Route(PATH, logto_webhook, methods=["POST"])]
