"""Billing (ADR 0043) — billing_runner : échéances MIT (Mollie recurring), dunning
borné, sweeps, réconciliation. Mollie + store monkeypatchés, logique pure testée."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oto_mcp import billing, billing_runner
from oto_mcp.db import billing as db_billing
from oto_mcp.mollie_client import MollieError

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _sub(**over) -> dict:
    base = {"org_id": 42, "plan": "solo", "method": "card",
            "mandate_id": "mdt_1", "customer_id": "cst_1",
            "current_period_end": NOW - timedelta(hours=2), "status": "active"}
    base.update(over)
    return base


def _wire(monkeypatch, *, attempts_before=0, payment=None, payment_exc=None):
    state = {"journal": [], "updates": [], "schedule": None, "retry": None,
             "status": None}
    monkeypatch.setattr(db_billing, "count_renewal_attempts",
                        lambda org, since: attempts_before)
    monkeypatch.setattr(db_billing, "insert_billing_payment",
                        lambda *a, **k: state["journal"].append((a, k)) or 11)
    monkeypatch.setattr(db_billing, "update_billing_payment",
                        lambda rid, **k: state["updates"].append((rid, k)) or True)
    monkeypatch.setattr(db_billing, "schedule_next_billing",
                        lambda org, pe, nb: state.update(schedule=(org, pe, nb)) or True)
    monkeypatch.setattr(db_billing, "retry_billing_at",
                        lambda org, when: state.update(retry=(org, when)) or True)
    monkeypatch.setattr(db_billing, "set_subscription_status",
                        lambda org, st, **k: state.update(status=(org, st, k)) or True)

    def fake_payment(amount, **k):
        state["charge"] = (amount, k)
        if payment_exc:
            raise payment_exc
        return payment or {"id": "tr_r1", "status": "paid"}

    monkeypatch.setattr(billing_runner.mollie_client, "create_recurring_payment",
                        fake_payment)
    return state


# ── _charge_one ──────────────────────────────────────────────────────────────

def test_renewal_success_anchors_on_period_end(monkeypatch):
    state = _wire(monkeypatch)
    assert billing_runner._charge_one(_sub(), NOW) == "renewed"
    amount, kw = state["charge"]
    assert amount == billing.PLANS["solo"]["amount"]
    assert kw["customer_id"] == "cst_1" and kw["mandate_id"] == "mdt_1"
    # idempotency_key déterministe période+tentative (anti double-débit)
    assert kw["idempotency_key"] == "org42-2026-07-06-a1"
    org, period_end, next_at = state["schedule"]
    # ancré sur current_period_end (+1 mois calendaire), PAS sur l'heure du tick
    assert (period_end.year, period_end.month, period_end.day) == (2026, 8, 6)
    assert period_end == next_at


def test_renewal_pending_sepa_counts_as_renewed(monkeypatch):
    # un prélèvement SEPA soumis reste 'pending' plusieurs jours → on avance le
    # cycle (la réconciliation/webhook rattrape un rejet ultérieur).
    state = _wire(monkeypatch, payment={"id": "tr_r1", "status": "pending"})
    assert billing_runner._charge_one(_sub(method="sepa"), NOW) == "renewed"
    assert state["schedule"] is not None


def test_renewal_far_overdue_catches_up(monkeypatch):
    state = _wire(monkeypatch)
    old = _sub(current_period_end=NOW - timedelta(days=70))
    billing_runner._charge_one(old, NOW)
    assert state["schedule"][1] > NOW          # jamais une échéance dans le passé


def test_failure_schedules_retry(monkeypatch):
    state = _wire(monkeypatch, attempts_before=0,
                  payment={"id": "tr_r1", "status": "failed"})
    assert billing_runner._charge_one(_sub(), NOW) == "retry"
    assert state["retry"] == (42, NOW + billing_runner._RETRY_DELAY)
    assert state["status"] is None             # pas encore past_due


def test_third_failure_goes_past_due_with_grace(monkeypatch):
    state = _wire(monkeypatch, attempts_before=2, payment_exc=MollieError(422, "declined"))
    assert billing_runner._charge_one(_sub(), NOW) == "past_due"
    org, st, kw = state["status"]
    assert (org, st) == (42, "past_due")
    assert kw["grace_until"] == NOW + billing_runner._GRACE
    # l'échec est journalisé (audit du dunning)
    assert state["updates"][-1][1]["status"] == "failed"


def test_unknown_plan_or_missing_mandate_skips(monkeypatch):
    state = _wire(monkeypatch)
    assert billing_runner._charge_one(_sub(plan="gold"), NOW) == "skipped"
    assert billing_runner._charge_one(_sub(mandate_id=None), NOW) == "skipped"
    assert "charge" not in state               # aucun débit tenté


# ── réconciliation ───────────────────────────────────────────────────────────

def test_reconcile_payment_updates_status(monkeypatch):
    updates = []
    monkeypatch.setattr(db_billing, "update_billing_payment",
                        lambda rid, **k: updates.append((rid, k)) or True)
    monkeypatch.setattr(billing_runner.mollie_client, "get_payment",
                        lambda pid: {"status": "paid"})
    billing_runner._reconcile_one({"id": 5, "kind": "renewal", "payment_id": "tr_r1",
                                   "status": "pending"}, NOW)
    assert updates == [(5, {"status": "paid"})]


def test_reconcile_paid_initial_replays_confirm(monkeypatch):
    called = {}
    monkeypatch.setattr(billing_runner.mollie_client, "get_payment",
                        lambda i: {"status": "paid"})
    monkeypatch.setattr(billing_runner.billing, "confirm",
                        lambda org: called.update(org=org))
    billing_runner._reconcile_one({"id": 5, "org_id": 42, "kind": "initial",
                                   "payment_id": None, "payment_intent_id": "tr_1",
                                   "status": "open"}, NOW)
    assert called["org"] == 42                 # onglet fermé → rattrapage miroir


def test_reconcile_stale_initial_payment_expires(monkeypatch):
    updates = []
    monkeypatch.setattr(db_billing, "update_billing_payment",
                        lambda rid, **k: updates.append((rid, k)) or True)
    monkeypatch.setattr(billing_runner.mollie_client, "get_payment",
                        lambda i: {"status": "open"})
    billing_runner._reconcile_one(
        {"id": 5, "org_id": 42, "kind": "initial", "payment_id": None,
         "payment_intent_id": "tr_1", "status": "open",
         "created_at": NOW - timedelta(hours=72)}, NOW)
    assert updates == [(5, {"status": "expired"})]


# ── tick ─────────────────────────────────────────────────────────────────────

def test_tick_noop_without_key(monkeypatch):
    monkeypatch.setattr(billing_runner.mollie_client, "is_configured", lambda: False)
    assert billing_runner.tick() == {}


def test_tick_sweeps_and_counts(monkeypatch):
    monkeypatch.setattr(billing_runner.mollie_client, "is_configured", lambda: True)
    monkeypatch.setattr(db_billing, "sweep_period_end_cancellations", lambda: [1])
    monkeypatch.setattr(db_billing, "sweep_grace_expired", lambda: [2, 3])
    monkeypatch.setattr(db_billing, "due_subscriptions", lambda: [])
    monkeypatch.setattr(db_billing, "open_billing_payments", lambda: [])
    assert billing_runner.tick() == {"closed": 3}


def test_runner_loop_registered_at_boot():
    # le lifespan du serveur embarque la boucle (gatée OTO_BILLING_RUNNER_ENABLED)
    import inspect
    from oto_mcp import server

    src = inspect.getsource(server.main)
    assert "billing_runner.run_billing_loop" in src
    assert "OTO_BILLING_RUNNER_ENABLED" in src
