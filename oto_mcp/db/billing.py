"""Store de l'abonnement par org (ADR 0043) — miroir + machine à états.

Deux tables : `org_subscriptions` (≤1 ligne par org, la vérité du cycle — Stancer
n'a ni webhooks ni subscription API, c'est NOTRE runner qui pilote) et
`billing_payments` (journal des échéances : audit, UI, file de réconciliation).

Les statuts de `billing_payments` sont les statuts Stancer OBSERVÉS (payment
intent puis payment) ; les statuts TERMINAUX sont figés ici (`TERMINAL_PAYMENT_
STATUSES`) — la file de réconciliation (`open_billing_payments`) = tout le reste.
"""
from __future__ import annotations

from typing import Any, Optional

from ._conn import _connect

# Statuts Stancer au-delà desquels un paiement ne bouge plus (union des enums
# PaymentIntentStatus/StatusCode du spec OpenAPI v2 — doit rester aligné avec
# l'index partiel idx_billing_payments_open de _schema.py).
TERMINAL_PAYMENT_STATUSES = frozenset(
    {"captured", "canceled", "refused", "failed", "expired", "unpaid"}
)

SUBSCRIPTION_STATUSES = ("active", "past_due", "canceled")


# ── org_subscriptions ────────────────────────────────────────────────────────

def get_org_subscription(org_id: int) -> Optional[dict]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM org_subscriptions WHERE org_id = %s", (org_id,)
        ).fetchone()


def upsert_org_subscription(
    org_id: int,
    *,
    plan: str,
    method: str = "card",
    provider: str = "stancer",
    customer_id: Optional[str] = None,
    card_id: Optional[str] = None,
    sepa_id: Optional[str] = None,
    mandate_rum: Optional[str] = None,
    status: str = "active",
    current_period_end: Optional[str] = None,
    next_billing_at: Optional[str] = None,
) -> None:
    """Crée ou remplace l'abonnement de l'org (souscription / re-souscription).

    Remplacement TOTAL assumé (re-souscrire après résiliation repart propre) —
    les mises à jour ciblées du cycle passent par les setters dédiés ci-dessous.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_subscriptions
                (org_id, provider, customer_id, card_id, sepa_id, mandate_rum,
                 method, plan, status, current_period_end, next_billing_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (org_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                customer_id = EXCLUDED.customer_id,
                card_id = EXCLUDED.card_id,
                sepa_id = EXCLUDED.sepa_id,
                mandate_rum = EXCLUDED.mandate_rum,
                method = EXCLUDED.method,
                plan = EXCLUDED.plan,
                status = EXCLUDED.status,
                current_period_end = EXCLUDED.current_period_end,
                next_billing_at = EXCLUDED.next_billing_at,
                grace_until = NULL,
                canceled_at = NULL,
                updated_at = NOW()
            """,
            (org_id, provider, customer_id, card_id, sepa_id, mandate_rum,
             method, plan, status, current_period_end, next_billing_at),
        )


def set_subscription_status(
    org_id: int,
    status: str,
    *,
    grace_until: Optional[str] = None,
    canceled: bool = False,
) -> bool:
    """Fait avancer la machine à états. `canceled=True` stampe `canceled_at`."""
    if status not in SUBSCRIPTION_STATUSES:
        raise ValueError(f"statut d'abonnement inconnu : {status!r}")
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET status = %s, grace_until = %s, "
            "canceled_at = CASE WHEN %s THEN NOW() ELSE canceled_at END, "
            "updated_at = NOW() WHERE org_id = %s",
            (status, grace_until, canceled, org_id),
        ).rowcount
    return n > 0


def set_subscription_card(org_id: int, card_id: str) -> bool:
    """Rotation du moyen de paiement (nouvelle carte tokenisée)."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET card_id = %s, method = 'card', "
            "updated_at = NOW() WHERE org_id = %s",
            (card_id, org_id),
        ).rowcount
    return n > 0


def mark_cancel_at_period_end(org_id: int) -> bool:
    """Résiliation à fin de période : stampe `canceled_at`, coupe la prochaine
    échéance. Le statut RESTE `active` (entitlement jusqu'à current_period_end) —
    la bascule finale est l'affaire du billing_runner."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET canceled_at = NOW(), "
            "next_billing_at = NULL, updated_at = NOW() "
            "WHERE org_id = %s AND status != 'canceled'",
            (org_id,),
        ).rowcount
    return n > 0


def schedule_next_billing(
    org_id: int, current_period_end: str, next_billing_at: str
) -> bool:
    """Avance le cycle après une échéance encaissée (retour à `active`)."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET current_period_end = %s, "
            "next_billing_at = %s, status = 'active', grace_until = NULL, "
            "updated_at = NOW() WHERE org_id = %s",
            (current_period_end, next_billing_at, org_id),
        ).rowcount
    return n > 0


def due_subscriptions(limit: int = 50) -> list[dict]:
    """Échéances à tirer par le billing_runner (actives ou en retard, dues)."""
    with _connect() as conn:
        return list(conn.execute(
            "SELECT * FROM org_subscriptions "
            "WHERE status IN ('active', 'past_due') AND next_billing_at <= NOW() "
            "ORDER BY next_billing_at ASC LIMIT %s",
            (limit,),
        ))


def active_subscription_plans() -> dict[int, str]:
    """org_id → plan des abonnements OUVRANT l'entitlement (active + grace).

    `past_due` reste entitled tant que la grace court — la fermeture est un acte
    du runner (passage à `canceled`), jamais une lecture qui décide.
    """
    with _connect() as conn:
        return {
            r["org_id"]: r["plan"]
            for r in conn.execute(
                "SELECT org_id, plan FROM org_subscriptions "
                "WHERE status = 'active' "
                "   OR (status = 'past_due' AND grace_until > NOW())"
            )
        }


def subscription_plan_for_org(org_id: int) -> Optional[str]:
    """Plan ouvrant l'entitlement pour CETTE org (même règle que ci-dessus)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT plan FROM org_subscriptions WHERE org_id = %s AND ("
            "status = 'active' OR (status = 'past_due' AND grace_until > NOW()))",
            (org_id,),
        ).fetchone()
    return row["plan"] if row else None


# ── billing_payments (journal) ───────────────────────────────────────────────

def insert_billing_payment(
    org_id: int,
    kind: str,
    amount: int,
    *,
    currency: str = "eur",
    payment_intent_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    status: str = "processing",
    attempt: int = 1,
) -> int:
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO billing_payments (org_id, kind, amount, currency, "
            "payment_intent_id, payment_id, status, attempt) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (org_id, kind, amount, currency, payment_intent_id, payment_id,
             status, attempt),
        ).fetchone()
    return int(row["id"])


def update_billing_payment(
    payment_row_id: int,
    *,
    status: str,
    payment_id: Optional[str] = None,
) -> bool:
    with _connect() as conn:
        n = conn.execute(
            "UPDATE billing_payments SET status = %s, "
            "payment_id = COALESCE(%s, payment_id), updated_at = NOW() "
            "WHERE id = %s",
            (status, payment_id, payment_row_id),
        ).rowcount
    return n > 0


def list_billing_payments(org_id: int, limit: int = 20) -> list[dict]:
    with _connect() as conn:
        return list(conn.execute(
            "SELECT * FROM billing_payments WHERE org_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (org_id, limit),
        ))


def open_billing_payments(limit: int = 100) -> list[dict]:
    """File de réconciliation : paiements non terminaux à re-poller (Stancer
    sans webhooks — c'est CE crochet qui rattrape les fermetures de navigateur
    post-3DS et les statuts en vol)."""
    placeholders = ",".join(["%s"] * len(TERMINAL_PAYMENT_STATUSES))
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT * FROM billing_payments WHERE status NOT IN ({placeholders}) "
            "ORDER BY created_at ASC LIMIT %s",
            (*TERMINAL_PAYMENT_STATUSES, limit),
        ))
