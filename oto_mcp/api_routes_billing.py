"""Webhook Mollie (ADR 0043) — réconciliation événementielle des paiements.

`POST /api/billing/webhook` — **non authentifié** (Mollie l'appelle sans JWT).
Modèle Mollie : le corps ne porte QUE l'id du paiement (`id=tr_…`, form-encodé) ;
on re-fetch l'objet avec NOTRE clé API → aucune confiance dans le POST (un id
forgé/inconnu ne déclenche rien). Complète le polling du billing_runner (le socle),
il ne le remplace pas. Toujours 200 : Mollie retente sur non-2xx, et un id qu'on
ne suit pas n'est pas une erreur.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from . import billing, mollie_client


def make_routes(options_handler: Callable[[Request], Awaitable[Response]]) -> list[Route]:

    async def webhook(request: Request) -> Response:
        # billing non configuré (dormant / clé absente) → no-op silencieux.
        if not mollie_client.is_configured():
            return PlainTextResponse("ok")
        try:
            form = await request.form()
        except Exception:
            return PlainTextResponse("ok")
        payment_id = (form.get("id") or "").strip()
        if not payment_id:
            return PlainTextResponse("ok")   # ping / corps vide
        try:
            # DB + httpx sync → hors event loop (serveur mono-loop).
            await run_in_threadpool(billing.process_webhook, payment_id)
        except mollie_client.MollieError:
            # amont Mollie en erreur (id disparu, 5xx) : on absorbe, le polling
            # rattrapera ; répondre 200 évite une tempête de retries Mollie.
            pass
        return PlainTextResponse("ok")

    return [
        Route("/api/billing/webhook", webhook, methods=["POST"]),
        Route("/api/billing/webhook", options_handler, methods=["OPTIONS"]),
    ]
