"""Billing B2 (ADR 0043) — machine à états subscribe → confirm → cancel.

Stancer et le store sont monkeypatchés : on teste la LOGIQUE du cycle (le
chemin sandbox réel a été exercé au smoke B2 du 2026-07-06 : ping, customer,
intent+url, tokenisation, MIT, idempotence 409)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oto_mcp import billing
from oto_mcp.db import billing as db_billing


# ── période calendaire ───────────────────────────────────────────────────────

def test_add_period_month_end_clamps():
    d = datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc)
    nxt = billing._add_period(d, "month")
    assert (nxt.year, nxt.month, nxt.day) == (2026, 2, 28)


def test_add_period_year_and_december():
    d = datetime(2026, 12, 15, tzinfo=timezone.utc)
    assert billing._add_period(d, "month").month == 1
    assert billing._add_period(d, "month").year == 2027
    assert billing._add_period(d, "year").year == 2027


# ── subscribe ────────────────────────────────────────────────────────────────

def _wire_subscribe(monkeypatch, existing=None):
    calls = {}
    monkeypatch.setattr(db_billing, "get_org_subscription", lambda org: existing)
    monkeypatch.setattr(db_billing, "insert_billing_payment",
                        lambda *a, **k: calls.setdefault("insert", (a, k)) or 1)
    def fake_customer(**k):
        calls["customer"] = k
        return {"id": "cust_1"}

    def fake_intent(amount, **k):
        calls["intent"] = (amount, k)
        return {"id": "pi_1", "status": "require_payment_method",
                "url": "https://payment.stancer.com/test_pi_1"}

    monkeypatch.setattr(billing.stancer_client, "create_customer", fake_customer)
    monkeypatch.setattr(billing.stancer_client, "create_payment_intent", fake_intent)
    return calls


def test_subscribe_happy_path(monkeypatch):
    calls = _wire_subscribe(monkeypatch)
    out = billing.subscribe(42, "solo", "https://oto.cx/billing")
    assert out["checkout_url"].startswith("https://payment.stancer.com/")
    assert calls["customer"]["external_id"] == "org-42"
    amount, kw = calls["intent"]
    assert amount == billing.PLANS["solo"]["amount"]
    assert kw["order_id"].startswith("org42:solo:")   # le plan voyage dans l'intent
    assert calls["insert"][0][:2] == (42, "initial")


def test_subscribe_rejects_unknown_plan_and_double(monkeypatch):
    _wire_subscribe(monkeypatch)
    with pytest.raises(ValueError, match="unknown_plan"):
        billing.subscribe(42, "gold", "https://oto.cx/billing")
    _wire_subscribe(monkeypatch, existing={"status": "active", "canceled_at": None,
                                           "customer_id": "cust_1", "plan": "solo"})
    with pytest.raises(ValueError, match="already_subscribed"):
        billing.subscribe(42, "solo", "https://oto.cx/billing")


def test_subscribe_reuses_customer(monkeypatch):
    calls = _wire_subscribe(monkeypatch, existing={
        "status": "canceled", "canceled_at": "2026-07-01", "customer_id": "cust_old",
        "plan": "solo"})
    billing.subscribe(42, "solo", "https://oto.cx/billing")
    assert "customer" not in calls                      # pas de re-création
    assert calls["intent"][1]["customer"] == "cust_old"


# ── confirm ──────────────────────────────────────────────────────────────────

def _wire_confirm(monkeypatch, *, intent, payments=None, sub=None):
    state = {}
    row = {"id": 7, "kind": "initial", "status": "processing",
           "payment_intent_id": "pi_1"}
    monkeypatch.setattr(db_billing, "get_org_subscription", lambda org: sub)
    monkeypatch.setattr(db_billing, "list_billing_payments", lambda org, limit=20: [row])
    monkeypatch.setattr(db_billing, "update_billing_payment",
                        lambda rid, **k: state.setdefault("update", (rid, k)) or True)
    monkeypatch.setattr(db_billing, "upsert_org_subscription",
                        lambda org, **k: state.setdefault("upsert", (org, k)))
    monkeypatch.setattr(billing.stancer_client, "get_payment_intent", lambda i: intent)
    monkeypatch.setattr(billing.stancer_client, "payment_intent_payments",
                        lambda i: {"payments": payments or []})
    monkeypatch.setattr(billing, "apply_plan_entitlements", lambda org, plan: None)
    return state


def test_confirm_pending_while_on_3ds(monkeypatch):
    _wire_confirm(monkeypatch, intent={"status": "require_authentication"})
    assert billing.confirm(42)["status"] == "pending"


def test_confirm_failure_closes_payment(monkeypatch):
    state = _wire_confirm(monkeypatch, intent={"status": "unpaid"})
    assert billing.confirm(42)["status"] == "failed"
    assert state["update"][1]["status"] == "unpaid"
    assert "upsert" not in state                        # jamais de miroir sur échec


def test_confirm_success_opens_subscription(monkeypatch):
    state = _wire_confirm(
        monkeypatch,
        intent={"status": "captured", "customer": "cust_1",
                "order_id": "org42:solo:abcd1234"},
        payments=[{"id": "paym_1", "card": {"id": "card_1"}}])
    out = billing.confirm(42)
    assert out["status"] == "active" and out["plan"] == "solo"
    org, kw = state["upsert"]
    assert (org, kw["plan"], kw["card_id"], kw["status"]) == (42, "solo", "card_1", "active")
    assert kw["next_billing_at"] == kw["current_period_end"]


def test_confirm_without_card_token_refuses(monkeypatch):
    # fonds encaissés mais pas de token → PAS d'abonnement irrenouvelable posé.
    _wire_confirm(monkeypatch,
                  intent={"status": "captured", "order_id": "org42:solo:x"},
                  payments=[{"id": "paym_1"}])
    with pytest.raises(RuntimeError, match="no_card_token"):
        billing.confirm(42)


def test_confirm_idempotent_when_active(monkeypatch):
    monkeypatch.setattr(db_billing, "get_org_subscription",
                        lambda org: {"status": "active", "plan": "solo"})
    monkeypatch.setattr(db_billing, "list_billing_payments", lambda org, limit=20: [])
    assert billing.confirm(42) == {"status": "active", "plan": "solo"}


# ── cancel & entitlement helper ──────────────────────────────────────────────

def test_cancel_requires_subscription(monkeypatch):
    monkeypatch.setattr(db_billing, "get_org_subscription", lambda org: None)
    with pytest.raises(ValueError, match="not_subscribed"):
        billing.cancel(42)


def test_plan_options_mapping():
    assert "unipile" in billing.plan_options("solo")
    assert billing.plan_options("inconnu") == frozenset()


# ── capacités ────────────────────────────────────────────────────────────────

def test_capabilities_registered_rest_only():
    from oto_mcp.capabilities.registry import CAPABILITIES

    caps = {c.key: c for c in CAPABILITIES if c.key.startswith("billing.")}
    assert set(caps) == {"billing.plans", "billing.status", "billing.subscribe",
                         "billing.confirm", "billing.cancel", "billing.payments",
                         "billing.admin_set_plan"}
    # pas d'URL de paiement dans un contexte LLM : seule la capacité ADMIN
    # (forcer un plan, pas de paiement) a une face MCP.
    mcp_caps = {k for k, c in caps.items() if c.mcp is not None}
    assert mcp_caps == {"billing.admin_set_plan"}
