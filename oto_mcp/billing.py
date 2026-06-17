"""Intégration Stripe — achat de packs de credits d'appel (paiement ponctuel).

Pas d'abonnement récurrent : un user d'une org achète un PACK de credits via
Stripe Checkout (`mode=payment`), et le webhook `checkout.session.completed`
crédite le wallet de l'org (`credits_store.credit`, idempotent sur l'event id).

La dégressivité du prix (1 ct → 0,1 ct par appel) est portée par la TAILLE du
pack (remise volume), pas par un calcul de palier. Catalogue `PACKS` en code
(prix ad-hoc via `price_data` — aucun Stripe Product/Price à pré-créer).

Le SDK `stripe` est importé paresseusement (dépendance optionnelle au boot) : un
SDK absent ne casse que les endpoints billing, jamais le démarrage du serveur.
Secrets en env de process uniquement (jamais oto.config — OTO_CONFIG_DISABLE_SOPS=1).
"""
from __future__ import annotations

import logging
import os

from .config import require_env

logger = logging.getLogger(__name__)

# Catalogue des packs. `amount_cents` = prix EUR du pack ; `calls` = credits ajoutés.
# 1 000 calls = 10 € (1 ct/appel) · 10 000 = 75 € (0,75 ct) · 100 000 = 100 € (0,1 ct).
PACKS: dict[str, dict] = {
    "starter": {"calls": 1_000, "amount_cents": 1000, "label": "1 000 appels"},
    "growth": {"calls": 10_000, "amount_cents": 7500, "label": "10 000 appels"},
    "scale": {"calls": 100_000, "amount_cents": 10000, "label": "100 000 appels"},
}


def packs() -> list[dict]:
    """Catalogue public (id + calls + prix + label), pour l'UI de recharge."""
    return [{"pack_id": k, **v} for k, v in PACKS.items()]


def create_checkout_session(org_id: int, pack_id: str, sub: str) -> dict:
    """Crée une session Stripe Checkout pour `pack_id` créditée à `org_id`.

    `metadata` porte `org_id` + `calls` : le webhook est ainsi auto-suffisant
    (le montant crédité est figé à l'achat, indépendant d'une évolution de PACKS).
    Renvoie `{checkout_url}` (à ouvrir côté front).
    """
    pack = PACKS.get(pack_id)
    if pack is None:
        raise ValueError(f"pack inconnu {pack_id!r}")
    import stripe

    stripe.api_key = require_env("STRIPE_SECRET_KEY")
    dash = os.environ.get("OTO_DASHBOARD_URL", "https://dashboard.oto.ninja").rstrip("/")
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": "eur",
                    "unit_amount": pack["amount_cents"],
                    "product_data": {"name": f"Oto credits — {pack['label']}"},
                },
            }
        ],
        metadata={
            "org_id": str(org_id),
            "pack_id": pack_id,
            "calls": str(pack["calls"]),
            "sub": sub,
        },
        success_url=f"{dash}/console/billing?status=success",
        cancel_url=f"{dash}/console/billing?status=cancel",
    )
    return {"checkout_url": session.url}


def verify_and_parse(payload: bytes, sig_header: str):
    """Vérifie la signature Stripe et parse l'event. Lève si invalide.

    `payload` DOIT être le corps BRUT (octets) — la signature couvre les octets
    exacts, ne jamais re-sérialiser via request.json().
    """
    import stripe

    secret = require_env("STRIPE_WEBHOOK_SECRET")
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def handle_event(event) -> None:
    """Applique un event Stripe vérifié. On n'agit que sur le paiement complété.

    Idempotent par construction : `credits_store.credit` rejette un rejeu via
    l'`UNIQUE(stripe_event_id)`.
    """
    if event["type"] != "checkout.session.completed":
        return
    md = (event["data"]["object"].get("metadata") or {})
    try:
        org_id = int(md["org_id"])
        calls = int(md["calls"])
    except (KeyError, ValueError):
        logger.warning("stripe webhook sans metadata org_id/calls exploitable: %s", md)
        return
    from . import credits_store

    res = credits_store.credit(org_id, calls, reason="stripe", stripe_event_id=event["id"])
    logger.info(
        "stripe top-up org=%s +%s calls (applied=%s, balance=%s)",
        org_id, calls, res.get("applied"), res.get("balance"),
    )
