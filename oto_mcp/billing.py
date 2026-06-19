"""Intégration Stripe — packs de credits d'appel (ponctuel) ET abonnement récurrent.

Deux modes coexistent :
- **Packs de credits** (`mode=payment`) : achat ponctuel, le webhook
  `checkout.session.completed` crédite le wallet de l'org (`credits_store.credit`).
- **Abonnement « option messagerie » (LinkedIn/WhatsApp)** (`mode=subscription`) : activer
  l'option = souscrire ; quantité = nb de comptes connectés (tous canaux), tarif **dégressif
  par paliers gradués** (15/10/7 €/mois). Le miroir local `org_subscriptions` (status/quantity)
  est tenu par les webhooks `customer.subscription.*` / `invoice.payment_failed` et gate
  l'activation. DISTINCT des credits : l'abonnement paie l'ACCÈS à l'option, les credits
  paient les APPELS (les deux cumulés).

La dégressivité des PACKS (1 ct → 0,1 ct par appel) est portée par la TAILLE du pack
(remise volume) via `price_data` inline. L'abonnement messagerie, lui, utilise un vrai
Stripe Price à **paliers gradués** (les tiers ne sont PAS exprimables en `price_data`
inline) — créé idempotemment via lookup_key (`_unipile_price_id`).

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


# Tarif dégressif par compte connecté (siège), paliers GRADUÉS par org : 1er compte
# 15 €, 2e 10 €, 3e+ 7 €/mois. Stripe calcule 15 + 10 + 7×(n−2) sur la quantité.
# Aligne notre marge sur le coût Unipile (~5 €/compte, plancher 49 €).
_UNIPILE_TIERS = [
    {"up_to": 1, "unit_amount": 1500},
    {"up_to": 2, "unit_amount": 1000},
    {"up_to": "inf", "unit_amount": 700},
]
_UNIPILE_PRICE_LOOKUP_KEY = "oto_unipile_seat_graduated_v1"


def _unipile_price_id() -> str:
    """Id du Stripe Price à paliers gradués pour les sièges Unipile. Idempotent via
    lookup_key : cherché, sinon créé au 1er besoin (pas de Price à pré-provisionner).
    `price_data` inline ne supporte PAS les tiers → un vrai Price object est requis."""
    import stripe

    stripe.api_key = require_env("STRIPE_SECRET_KEY")
    found = stripe.Price.list(lookup_keys=[_UNIPILE_PRICE_LOOKUP_KEY], limit=1)["data"]
    if found:
        return found[0]["id"]
    price = stripe.Price.create(
        lookup_key=_UNIPILE_PRICE_LOOKUP_KEY,
        currency="eur",
        recurring={"interval": "month"},
        billing_scheme="tiered",
        tiers_mode="graduated",
        tiers=_UNIPILE_TIERS,
        product_data={"name": "Oto — option messagerie hébergée (LinkedIn/WhatsApp)"},
    )
    return price["id"]


def create_unipile_subscription_checkout(org_id: int, sub: str, quantity: int = 1) -> dict:
    """Crée un Stripe Checkout `mode=subscription` pour l'option LinkedIn de l'org
    (€15/mois × sièges). `metadata` sur la session ET l'abonnement → les webhooks
    `customer.subscription.*` portent `org_id`/`product`. Renvoie `{checkout_url}`."""
    import stripe

    stripe.api_key = require_env("STRIPE_SECRET_KEY")
    dash = os.environ.get("OTO_DASHBOARD_URL", "https://dashboard.oto.ninja").rstrip("/")
    qty = max(1, int(quantity))
    md = {"org_id": str(org_id), "product": "unipile"}
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": _unipile_price_id(), "quantity": qty}],
        metadata={**md, "sub": sub},
        subscription_data={"metadata": md},
        success_url=f"{dash}/console/connections?unipile=subscribed",
        cancel_url=f"{dash}/console/connections?unipile=cancel",
    )
    return {"checkout_url": session.url}


def sync_unipile_seats(org_id: int) -> None:
    """Aligne la quantité de l'abonnement Stripe sur le nb de comptes connectés de
    l'org (Stripe prorate). No-op si pas d'abonnement actif. Best-effort (appelé
    après connect/disconnect — ne doit pas faire échouer l'opération)."""
    from . import db

    s = db.get_org_subscription(org_id, "unipile")
    if not s or not s.get("stripe_subscription_id"):
        return
    if s.get("status") not in ("active", "trialing", "past_due"):
        return
    qty = max(1, db.count_unipile_accounts_for_org(org_id))
    if qty == s.get("quantity"):
        return
    try:
        import stripe

        stripe.api_key = require_env("STRIPE_SECRET_KEY")
        sub_obj = stripe.Subscription.retrieve(s["stripe_subscription_id"])
        item_id = sub_obj["items"]["data"][0]["id"]
        stripe.Subscription.modify(
            s["stripe_subscription_id"],
            items=[{"id": item_id, "quantity": qty}],
            proration_behavior="create_prorations",
        )
        db.upsert_org_subscription(org_id, "unipile", status=s["status"], quantity=qty)
        logger.info("unipile seats synced org=%s qty=%s", org_id, qty)
    except Exception:
        logger.warning("unipile seat sync skipped org=%s", org_id, exc_info=True)


def has_active_unipile_subscription(org_id: int) -> bool:
    """L'org a-t-elle l'option LinkedIn payée et active (gate d'activation) ?
    `past_due` reste toléré (grâce de paiement) ; `canceled`/`inactive` non."""
    from . import db

    s = db.get_org_subscription(org_id, "unipile")
    return bool(s and s.get("status") in ("active", "trialing", "past_due"))


def verify_and_parse(payload: bytes, sig_header: str):
    """Vérifie la signature Stripe et parse l'event. Lève si invalide.

    `payload` DOIT être le corps BRUT (octets) — la signature couvre les octets
    exacts, ne jamais re-sérialiser via request.json().
    """
    import stripe

    secret = require_env("STRIPE_WEBHOOK_SECRET")
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def handle_event(event) -> None:
    """Applique un event Stripe vérifié. Dispatch packs (credits) vs abonnement
    (option LinkedIn). Idempotent par construction (credits: UNIQUE event_id ;
    abonnement: upsert du miroir status/quantity)."""
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "checkout.session.completed":
        if obj.get("mode") == "subscription":
            _on_subscription_checkout(obj)
        else:
            _on_pack_checkout(event, obj)
    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        _on_subscription_change(obj)
    elif etype == "customer.subscription.deleted":
        _on_subscription_status(obj, "canceled")
    elif etype == "invoice.payment_failed":
        _on_invoice_failed(obj)


def _on_pack_checkout(event, session) -> None:
    md = session.get("metadata") or {}
    try:
        org_id = int(md["org_id"])
        calls = int(md["calls"])
    except (KeyError, ValueError):
        logger.warning("stripe webhook pack sans metadata org_id/calls: %s", md)
        return
    from . import credits_store

    res = credits_store.credit(org_id, calls, reason="stripe", stripe_event_id=event["id"])
    logger.info("stripe top-up org=%s +%s calls (applied=%s, balance=%s)",
                org_id, calls, res.get("applied"), res.get("balance"))


def _sub_org_product(obj) -> tuple[int, str] | tuple[None, None]:
    """(org_id, product) d'un objet abonnement/session via metadata, sinon lookup
    par stripe_subscription_id."""
    md = obj.get("metadata") or {}
    if md.get("org_id") and md.get("product"):
        try:
            return int(md["org_id"]), md["product"]
        except ValueError:
            pass
    from . import db

    sub_id = obj.get("id") if obj.get("object") == "subscription" else obj.get("subscription")
    row = db.get_org_by_subscription_id(sub_id) if sub_id else None
    return (row["org_id"], row["product"]) if row else (None, None)


def _on_subscription_checkout(session) -> None:
    """Session subscription complétée → enregistre l'abonnement actif + ses ids."""
    from . import db

    md = session.get("metadata") or {}
    try:
        org_id, product = int(md["org_id"]), md["product"]
    except (KeyError, ValueError):
        logger.warning("stripe subscription checkout sans metadata: %s", md)
        return
    db.upsert_org_subscription(
        org_id, product, status="active",
        stripe_customer_id=session.get("customer"),
        stripe_subscription_id=session.get("subscription"),
    )
    logger.info("unipile subscription active org=%s product=%s", org_id, product)


def _on_subscription_change(sub) -> None:
    """customer.subscription.created/updated → miroir status + quantity."""
    from . import db

    org_id, product = _sub_org_product(sub)
    if org_id is None:
        return
    items = (sub.get("items") or {}).get("data") or []
    qty = items[0].get("quantity") if items else None
    db.upsert_org_subscription(
        org_id, product, status=sub.get("status", "active"),
        stripe_customer_id=sub.get("customer"),
        stripe_subscription_id=sub.get("id"),
        quantity=qty,
    )
    logger.info("unipile subscription org=%s status=%s qty=%s", org_id, sub.get("status"), qty)


def _on_subscription_status(sub, status) -> None:
    from . import db

    org_id, product = _sub_org_product(sub)
    if org_id is None:
        return
    db.upsert_org_subscription(org_id, product, status=status,
                               stripe_subscription_id=sub.get("id"))
    logger.info("unipile subscription org=%s → %s", org_id, status)


def _on_invoice_failed(invoice) -> None:
    from . import db

    org_id, product = _sub_org_product(invoice)
    if org_id is None:
        return
    db.upsert_org_subscription(org_id, product, status="past_due")
    logger.info("unipile invoice failed org=%s → past_due", org_id)
