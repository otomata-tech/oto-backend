"""Runner d'échéances d'abonnement (ADR 0043) — la « récurrence » maison.

Le miroir local fait foi : cette boucle de fond (lifespan, même famille que
scheduler.py) fait tout le cycle à intervalle horaire :

1. **Échéances dues** (`due_subscriptions`) : rejoue un paiement MIT
   (`sequenceType=recurring`) sur `customerId`+`mandateId`. `Idempotency-Key`
   DÉTERMINISTE `org<id>-<période>-a<tentative>` → un tick concurrent/rejoué
   renvoie le MÊME paiement Mollie (HTTP 200), jamais un double débit.
2. **Politique d'impayé** (dunning borné) : échec → retry à J+3 (tentatives
   trackées par le JOURNAL, pas un compteur mutable) ; 3 échecs → `past_due`
   + grace 14 j. La notification org_admin = barreau ultérieur.
3. **Sweeps** : résiliations à période échue + graces consommées → `canceled`
   (c'est la fermeture d'entitlement ; les données ne bougent jamais).
4. **Réconciliation** (`open_billing_payments`) : re-polle les paiements non
   terminaux (checkout fermé post-paiement, prélèvement SEPA qui se dénoue en
   plusieurs jours…) ; un premier paiement jamais encaissé finit `expired`
   (Mollie expire les paiements ouverts ; garde-fou TTL 48 h en secours).

Sans MOLLIE_API_KEY le tick est un no-op silencieux (le serveur vit sans
billing). Un tick raté ne tue jamais la boucle.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import billing, mollie_client
from .db import billing as db_billing

log = logging.getLogger("oto_mcp.billing_runner")

_POLL_INTERVAL_S = 3600
_RETRY_DELAY = timedelta(days=3)
_MAX_ATTEMPTS = 3
_GRACE = timedelta(days=14)
_INITIAL_INTENT_TTL = timedelta(hours=48)

# statuts Mollie d'un paiement MIT qui valent encaissement (ou en cours de
# dénouement — `pending` = prélèvement SEPA soumis ; la réconciliation/webhook
# rattrape un éventuel rejet ultérieur).
_PAYMENT_OK = frozenset({"paid", "pending", "authorized"})
_PAYMENT_FAILED = frozenset({"failed", "canceled", "expired"})


def _charge_one(sub_row: dict, now: datetime) -> str:
    """Tire l'échéance d'UN abonnement. Retourne l'issue (log/test) :
    'renewed' | 'retry' | 'past_due' | 'skipped'."""
    org_id = sub_row["org_id"]
    if sub_row.get("provider") == "comp":
        # abonnement FORCÉ par un admin (non payé) — jamais de débit. Ceinture
        # + bretelles : due_subscriptions l'exclut déjà (next_billing_at NULL).
        return "skipped"
    plan = billing.PLANS.get(sub_row["plan"])
    if plan is None:
        log.error("billing_runner: org %s a un plan inconnu %r — échéance sautée",
                  org_id, sub_row["plan"])
        return "skipped"
    if not sub_row.get("customer_id") or not sub_row.get("mandate_id"):
        log.error("billing_runner: org %s sans customer/mandat rejouable "
                  "(method=%s) — sautée", org_id, sub_row.get("method"))
        return "skipped"

    period_ref = str(sub_row.get("current_period_end") or "epoch")[:10]
    attempt = db_billing.count_renewal_attempts(
        org_id, sub_row.get("current_period_end") or now) + 1
    idempotency_key = f"org{org_id}-{period_ref}-a{attempt}"

    row_id = db_billing.insert_billing_payment(
        org_id, "renewal", plan["amount"], currency=plan["currency"],
        status="processing", attempt=attempt)
    try:
        payment = mollie_client.create_recurring_payment(
            plan["amount"], customer_id=sub_row["customer_id"],
            mandate_id=sub_row["mandate_id"], currency=plan["currency"],
            idempotency_key=idempotency_key, webhook_url=billing.webhook_url(),
            description=f"Abonnement {plan['label']} — échéance {period_ref}")
        pstatus = str(payment.get("status") or "")
        db_billing.update_billing_payment(row_id, status=pstatus or "processing",
                                          payment_id=payment.get("id"))
    except mollie_client.MollieError as e:
        db_billing.update_billing_payment(row_id, status="failed")
        log.warning("billing_runner: org %s échéance refusée (Mollie %s)",
                    org_id, e.status_code)
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
    """Re-polle UN paiement non terminal du journal (initial ou renewal — tous
    des objets `payment` Mollie `tr_`)."""
    ref = row.get("payment_id") or row.get("payment_intent_id")
    if not ref:
        return
    status = str(mollie_client.get_payment(ref).get("status") or "")
    if row.get("kind") == "initial" and status == "paid":
        # encaissé sur la page hébergée sans que confirm ait tourné (onglet
        # fermé) : on termine la pose du miroir nous-mêmes.
        try:
            billing.confirm(row["org_id"])
        except Exception as e:
            log.warning("billing_runner: confirm de rattrapage org %s : %s",
                        row["org_id"], e)
        return
    if status and status != row["status"]:
        db_billing.update_billing_payment(row["id"], status=status)
        return
    # premier paiement resté ouvert trop longtemps → expiré (garde-fou ; Mollie
    # expire de lui-même, ce TTL couvre un statut en vol).
    if row.get("kind") == "initial" and status not in mollie_client.TERMINAL_PAYMENT_STATUSES:
        created = row.get("created_at")
        created_dt = created if isinstance(created, datetime) else None
        if created_dt and now - created_dt > _INITIAL_INTENT_TTL:
            db_billing.update_billing_payment(row["id"], status="expired")


def tick() -> dict:
    """Un passage complet (sync, appelé en thread). Retourne les compteurs."""
    if not mollie_client.is_configured():
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
        except mollie_client.MollieError as e:
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
