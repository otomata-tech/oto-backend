"""Billing par org (ADR 0043, B2) — abonnement unique, PSP Stancer.

Le cycle est piloté ICI (Stancer n'a ni webhooks ni subscription API) :
- `subscribe` ouvre le paiement initial sur la page hébergée Stancer
  (tokenisation + 3DS gérés par eux) et journalise l'intent ;
- `confirm` POLLE l'intent au retour du payeur (et en réconciliation), extrait
  le token carte du paiement encaissé et pose le miroir `org_subscriptions`
  à `active` — c'est LUI qui ouvre l'entitlement, jamais le redirect brut ;
- `cancel` marque la résiliation à fin de période (l'entitlement court jusqu'à
  `current_period_end` ; le billing_runner (B3) fera la bascule).

Le plan (prix, options débloquées) vit dans `PLANS` — mapping en CODE (pas de
table) : la vérité produit est versionnée et relue par l'entitlement (has_option,
2e source). ⚠️ Valeurs actuelles = PLACEHOLDER sandbox — la décision produit
(prix réels, contenu, niveau gratuit) est un préalable au barreau B4 (ADR 0043).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import stancer_client
from .db import billing as db_billing

logger = logging.getLogger(__name__)

# plan → prix (centimes), intervalle, options de connecteur débloquées (couche 3,
# lues par access.has_option en B4). PLACEHOLDER — cf. docstring module.
PLANS: dict[str, dict] = {
    "standard": {
        "label": "Otomata Standard",
        "amount": 4900,
        "currency": "eur",
        "interval": "month",   # 'month' | 'year'
        "options": ("unipile",),
    },
}

# Statuts d'intent qui signifient « fonds obtenus ou en cours de capture »
# (capture=true par défaut : authorized → captured suit en batch Stancer).
_INTENT_SUCCESS = frozenset({"authorized", "captured"})
_INTENT_FAILED = frozenset({"canceled", "unpaid"})


def plans() -> list[dict]:
    """Catalogue public (l'UI billing du dashboard boucle dessus)."""
    return [{"plan": k, **{f: v[f] for f in ("label", "amount", "currency", "interval")}}
            for k, v in PLANS.items()]


def plan_options(plan: str) -> frozenset[str]:
    """Options de connecteur débloquées par `plan` (consommé par access.has_option)."""
    meta = PLANS.get(plan)
    return frozenset(meta["options"]) if meta else frozenset()


def _add_period(dt: datetime, interval: str) -> datetime:
    """Échéance suivante au mois/an CALENDAIRE (pas d'approximation 30 j) —
    borné au dernier jour du mois cible (31/01 + 1 mois → 28/02)."""
    if interval == "year":
        return _safe_replace(dt, year=dt.year + 1, month=dt.month)
    month = dt.month + 1
    year = dt.year + (1 if month > 12 else 0)
    return _safe_replace(dt, year=year, month=((month - 1) % 12) + 1)


def _safe_replace(dt: datetime, *, year: int, month: int) -> datetime:
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def _ref_id(value: Any) -> Optional[str]:
    """Les refs Stancer arrivent en id nu OU en objet embarqué selon l'endpoint."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return str(value)


# ── souscription ─────────────────────────────────────────────────────────────

def subscribe(org_id: int, plan: str, return_url: str) -> dict:
    """Ouvre la souscription : customer Stancer (réutilisé si l'org en a déjà
    un) + payment intent → renvoie l'URL de la page de paiement hébergée.
    Le miroir n'est PAS posé ici — il naît à `confirm` (paiement constaté)."""
    meta = PLANS.get(plan)
    if meta is None:
        raise ValueError(f"unknown_plan: {plan!r} (plans : {', '.join(PLANS)})")
    existing = db_billing.get_org_subscription(org_id)
    if existing and existing["status"] == "active" and not existing.get("canceled_at"):
        raise ValueError("already_subscribed: l'org a déjà un abonnement actif")

    customer_id = existing["customer_id"] if existing and existing.get("customer_id") else None
    if not customer_id:
        cust = stancer_client.create_customer(
            name=f"Otomata org {org_id}", external_id=f"org-{org_id}")
        customer_id = cust["id"]

    # le plan voyage dans l'order_id de l'intent (pas d'état serveur pendant le
    # checkout : confirm le relit de l'intent — survit à un restart).
    order_id = f"org{org_id}:{plan}:{uuid.uuid4().hex[:8]}"
    intent = stancer_client.create_payment_intent(
        meta["amount"], currency=meta["currency"], customer=customer_id,
        return_url=return_url, description=f"Abonnement {meta['label']}",
        order_id=order_id)
    db_billing.insert_billing_payment(
        org_id, "initial", meta["amount"], currency=meta["currency"],
        payment_intent_id=intent["id"], status=intent.get("status", "processing"))
    return {"checkout_url": intent.get("url"), "payment_intent_id": intent["id"],
            "plan": plan}


def confirm(org_id: int) -> dict:
    """Polle le dernier paiement initial ouvert de l'org et, si l'intent a
    encaissé, pose le miroir `active` (extraction du token carte du paiement).
    Idempotent : re-confirmer un abonnement déjà actif est un no-op informatif."""
    sub_row = db_billing.get_org_subscription(org_id)
    open_initial = [
        p for p in db_billing.list_billing_payments(org_id)
        if p["kind"] == "initial"
        and p["status"] not in db_billing.TERMINAL_PAYMENT_STATUSES
        and p.get("payment_intent_id")
    ]
    if not open_initial:
        if sub_row and sub_row["status"] == "active":
            return {"status": "active", "plan": sub_row["plan"]}
        raise ValueError("no_pending_subscription: aucun paiement initial en cours")

    row = open_initial[0]  # le plus récent (list_billing_payments trie DESC)
    intent = stancer_client.get_payment_intent(row["payment_intent_id"])
    istatus = str(intent.get("status") or "")

    if istatus in _INTENT_FAILED:
        db_billing.update_billing_payment(row["id"], status=istatus)
        return {"status": "failed", "intent_status": istatus}
    if istatus not in _INTENT_SUCCESS:
        # pas terminal : le payeur est peut-être encore sur la page 3DS.
        return {"status": "pending", "intent_status": istatus}

    # encaissé → extraire le paiement + le token carte, poser le miroir.
    payments = stancer_client.payment_intent_payments(row["payment_intent_id"])
    plist = payments.get("payments") if isinstance(payments, dict) else payments
    first = (plist or [{}])[0]
    card_id = _ref_id(first.get("card")) or _ref_id(intent.get("card"))
    payment_id = first.get("id")
    if not card_id:
        # fonds obtenus mais pas de token → pas de récurrence possible : on ne
        # pose PAS un abonnement qu'on ne saura pas renouveler (ADR : jamais de
        # fallback silencieux). Cas à investiguer (tokenize sur la page hébergée).
        raise RuntimeError(
            "no_card_token: intent encaissé sans token carte réutilisable — "
            "récurrence impossible, vérifier la tokenisation de la page hébergée")

    order = str(intent.get("order_id") or "")
    plan = order.split(":")[1] if order.count(":") >= 2 else None
    if plan not in PLANS:
        raise RuntimeError(f"bad_order_id: plan illisible sur l'intent ({order!r})")
    meta = PLANS[plan]

    now = datetime.now(timezone.utc)
    period_end = _add_period(now, meta["interval"])
    db_billing.update_billing_payment(row["id"], status="captured" if istatus == "captured" else "to_capture",
                                      payment_id=payment_id)
    db_billing.upsert_org_subscription(
        org_id, plan=plan, method="card",
        customer_id=_ref_id(intent.get("customer")), card_id=card_id,
        status="active", current_period_end=period_end, next_billing_at=period_end)
    logger.info("billing: org %s abonnée (plan %s, échéance %s)", org_id, plan,
                period_end.date())
    return {"status": "active", "plan": plan,
            "current_period_end": period_end.isoformat()}


# ── état & résiliation ───────────────────────────────────────────────────────

def status(org_id: int) -> dict:
    row = db_billing.get_org_subscription(org_id)
    if not row:
        return {"subscribed": False, "plans": plans()}
    meta = PLANS.get(row["plan"], {})
    return {
        "subscribed": row["status"] in ("active", "past_due"),
        "plan": row["plan"], "label": meta.get("label"),
        "amount": meta.get("amount"), "currency": meta.get("currency"),
        "interval": meta.get("interval"),
        "status": row["status"], "method": row["method"],
        "current_period_end": row.get("current_period_end"),
        "next_billing_at": row.get("next_billing_at"),
        "grace_until": row.get("grace_until"),
        "canceled_at": row.get("canceled_at"),
    }


def cancel(org_id: int) -> dict:
    """Résiliation à fin de période : l'entitlement court jusqu'à
    `current_period_end`, plus aucune échéance n'est tirée (next_billing_at
    nettoyé) ; le billing_runner basculera le statut à l'échéance."""
    row = db_billing.get_org_subscription(org_id)
    if not row or row["status"] == "canceled":
        raise ValueError("not_subscribed: aucun abonnement à résilier")
    db_billing.mark_cancel_at_period_end(org_id)
    return status(org_id)
