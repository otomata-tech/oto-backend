"""Runner d'échéances d'abonnement (ADR 0043, B3) — la « récurrence » maison.

Stancer n'a ni webhooks ni subscription API : cette boucle de fond (lifespan,
même famille que scheduler.py) fait tout le cycle à intervalle horaire :

1. **Échéances dues** (`due_subscriptions`) : rejoue un paiement MIT sur le
   token carte. `unique_id` DÉTERMINISTE `org<id>-<période>-a<tentative>` →
   un tick concurrent/rejoué prend un 409 Stancer, jamais un double débit.
2. **Politique d'impayé** (dunning borné) : échec → retry à J+3 (tentatives
   trackées par le JOURNAL, pas un compteur mutable) ; 3 échecs → `past_due`
   + grace 14 j. La notification org_admin = B6.
3. **Sweeps** : résiliations à période échue + graces consommées → `canceled`
   (c'est la fermeture d'entitlement ; les données ne bougent jamais).
4. **Réconciliation** (`open_billing_payments`) : re-polle les paiements non
   terminaux (capture batch Stancer, navigateur fermé post-3DS…) ; un intent
   initial jamais payé est clos `expired` après 48 h.

Sans STANCER_API_KEY le tick est un no-op silencieux (le serveur vit sans
billing). Un tick raté ne tue jamais la boucle.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import billing, stancer_client
from .db import billing as db_billing

log = logging.getLogger("oto_mcp.billing_runner")

_POLL_INTERVAL_S = 3600
_RETRY_DELAY = timedelta(days=3)
_MAX_ATTEMPTS = 3
_GRACE = timedelta(days=14)
_INITIAL_INTENT_TTL = timedelta(hours=48)

# statuts Stancer d'un paiement MIT qui valent encaissement (la capture est un
# batch asynchrone côté Stancer — la réconciliation finalisera à `captured`).
_PAYMENT_OK = frozenset({"authorized", "to_capture", "capture_sent", "captured"})


def _charge_one(sub_row: dict, now: datetime) -> str:
    """Tire l'échéance d'UN abonnement. Retourne l'issue (log/test) :
    'renewed' | 'retry' | 'past_due' | 'skipped'."""
    org_id = sub_row["org_id"]
    plan = billing.PLANS.get(sub_row["plan"])
    if plan is None:
        log.error("billing_runner: org %s a un plan inconnu %r — échéance sautée",
                  org_id, sub_row["plan"])
        return "skipped"
    if sub_row.get("method") != "card" or not sub_row.get("card_id"):
        log.error("billing_runner: org %s sans token carte (method=%s) — sautée",
                  org_id, sub_row.get("method"))
        return "skipped"

    period_ref = str(sub_row.get("current_period_end") or "epoch")[:10]
    attempt = db_billing.count_renewal_attempts(
        org_id, sub_row.get("current_period_end") or now) + 1
    unique_id = f"org{org_id}-{period_ref}-a{attempt}"

    row_id = db_billing.insert_billing_payment(
        org_id, "renewal", plan["amount"], currency=plan["currency"],
        status="processing", attempt=attempt)
    try:
        payment = stancer_client.create_payment(
            plan["amount"], currency=plan["currency"],
            card=sub_row["card_id"], customer=sub_row.get("customer_id"),
            unique_id=unique_id,
            description=f"Abonnement {plan['label']} — échéance {period_ref}")
        pstatus = str(payment.get("status") or "")
        db_billing.update_billing_payment(row_id, status=pstatus or "processing",
                                          payment_id=payment.get("id"))
    except stancer_client.StancerError as e:
        if e.status_code == 409:
            # unique_id déjà joué (tick concurrent / crash après POST) : le
            # paiement existe côté Stancer — la réconciliation le rattrape.
            db_billing.update_billing_payment(row_id, status="processing")
            log.warning("billing_runner: org %s échéance déjà en vol (%s)",
                        org_id, unique_id)
            return "skipped"
        db_billing.update_billing_payment(row_id, status="failed")
        pstatus = "failed"

    if pstatus in _PAYMENT_OK:
        # ancrage CALENDAIRE sur la fin de période payée (pas sur la date du
        # tick — les retries J+3 ne décalent pas le cycle) ; si l'échéance a
        # traîné plus d'une période, on avance jusqu'à dépasser maintenant.
        base = sub_row.get("current_period_end")
        nxt = billing._add_period(base if isinstance(base, datetime) else now,
                                  plan["interval"])
        while nxt <= now:
            nxt = billing._add_period(nxt, plan["interval"])
        db_billing.schedule_next_billing(org_id, nxt, nxt)
        log.info("billing_runner: org %s renouvelée (plan %s → %s)",
                 org_id, sub_row["plan"], nxt.date())
        return "renewed"

    # échec — retry borné puis past_due + grace (fermeture au sweep).
    if attempt < _MAX_ATTEMPTS:
        db_billing.retry_billing_at(org_id, now + _RETRY_DELAY)
        log.warning("billing_runner: org %s échéance refusée (tentative %d/%d) — "
                    "retry %s", org_id, attempt, _MAX_ATTEMPTS,
                    (now + _RETRY_DELAY).date())
        return "retry"
    db_billing.set_subscription_status(org_id, "past_due",
                                       grace_until=now + _GRACE)
    log.warning("billing_runner: org %s en impayé (3 échecs) — grace jusqu'au %s",
                org_id, (now + _GRACE).date())
    return "past_due"


def _reconcile_one(row: dict, now: datetime) -> None:
    """Re-polle UN paiement non terminal du journal."""
    if row.get("payment_id"):
        status = str(stancer_client.get_payment(row["payment_id"]).get("status") or "")
        if status and status != row["status"]:
            db_billing.update_billing_payment(row["id"], status=status)
        return
    if row.get("payment_intent_id"):
        intent = stancer_client.get_payment_intent(row["payment_intent_id"])
        istatus = str(intent.get("status") or "")
        if istatus in ("captured", "authorized"):
            # payé sur la page hébergée sans que confirm ait tourné (onglet
            # fermé) : on termine la pose du miroir nous-mêmes.
            try:
                billing.confirm(row["org_id"])
            except Exception as e:
                log.warning("billing_runner: confirm de rattrapage org %s : %s",
                            row["org_id"], e)
            return
        if istatus in ("canceled", "unpaid"):
            db_billing.update_billing_payment(row["id"], status=istatus)
            return
        created = row.get("created_at")
        created_dt = created if isinstance(created, datetime) else None
        if created_dt and now - created_dt > _INITIAL_INTENT_TTL:
            db_billing.update_billing_payment(row["id"], status="expired")


def tick() -> dict:
    """Un passage complet (sync, appelé en thread). Retourne les compteurs."""
    if not stancer_client.is_configured():
        return {}
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    for org_id in db_billing.sweep_period_end_cancellations():
        log.info("billing_runner: org %s résiliée (période échue)", org_id)
        counts["closed"] = counts.get("closed", 0) + 1
    for org_id in db_billing.sweep_grace_expired():
        log.warning("billing_runner: org %s fermée (grace consommée)", org_id)
        counts["closed"] = counts.get("closed", 0) + 1

    for sub_row in db_billing.due_subscriptions():
        outcome = _charge_one(sub_row, now)
        counts[outcome] = counts.get(outcome, 0) + 1

    for row in db_billing.open_billing_payments():
        try:
            _reconcile_one(row, now)
            counts["reconciled"] = counts.get("reconciled", 0) + 1
        except stancer_client.StancerError as e:
            log.warning("billing_runner: réconciliation paiement %s : %s",
                        row.get("id"), e)
    return counts


async def run_billing_loop(interval: int = _POLL_INTERVAL_S) -> None:
    """Boucle de fond (lifespan) — un tick raté ne tue pas la boucle."""
    log.info("billing runner démarré (intervalle %ss)", interval)
    while True:
        try:
            counts = await asyncio.to_thread(tick)
            if counts:
                log.info("billing_runner tick : %s", counts)
        except asyncio.CancelledError:
            log.info("billing runner arrêté")
            raise
        except Exception as e:  # un tick raté ne tue pas la boucle
            log.warning("billing_runner tick échoué : %s", e)
        await asyncio.sleep(interval)
