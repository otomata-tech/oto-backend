"""Billing (ADR 0043) — machine à états subscribe → confirm → cancel, PSP Mollie.

Mollie et le store sont monkeypatchés : on teste la LOGIQUE du cycle (le chemin
sandbox réel a été exercé le 2026-07-24 : customer, first payment + checkout,
mandat, recurring + Idempotency-Key). Mollie unifie carte et SEPA derrière un
customer + un mandat né du premier paiement → UN seul chemin subscribe/confirm."""
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
        return {"id": "cst_1"}

    def fake_first_payment(amount, **k):
        calls["payment"] = (amount, k)
        return {"id": "tr_1", "status": "open",
                "_links": {"checkout": {"href": "https://www.mollie.com/checkout/tr_1"}}}

    monkeypatch.setattr(billing.mollie_client, "create_customer", fake_customer)
    monkeypatch.setattr(billing.mollie_client, "create_first_payment", fake_first_payment)
    return calls


def test_subscribe_happy_path(monkeypatch):
    calls = _wire_subscribe(monkeypatch)
    out = billing.subscribe(42, "solo", "https://otomata.tech/billing")
    assert out["checkout_url"].startswith("https://www.mollie.com/checkout/")
    assert calls["customer"]["metadata"] == {"org_id": "42"}
    amount, kw = calls["payment"]
    assert amount == billing.PLANS["solo"]["amount"]
    assert kw["method"] == "creditcard"                  # 'card' → page carte
    assert kw["metadata"] == {"org_id": "42", "plan": "solo"}   # le plan voyage
    assert calls["insert"][0][:2] == (42, "initial")


def test_subscribe_sepa_maps_to_directdebit(monkeypatch):
    # UN seul flux : method='sepa' restreint juste la page Mollie (mandat SEPA
    # collecté sur le checkout, plus de flux IBAN/OTP/ICS séparé).
    calls = _wire_subscribe(monkeypatch)
    out = billing.subscribe(42, "solo", "https://otomata.tech/billing", method="sepa")
    assert out["method"] == "sepa"
    assert calls["payment"][1]["method"] == "directdebit"


def test_subscribe_rejects_unknown_plan_method_and_double(monkeypatch):
    _wire_subscribe(monkeypatch)
    with pytest.raises(ValueError, match="unknown_plan"):
        billing.subscribe(42, "gold", "https://otomata.tech/billing")
    with pytest.raises(ValueError, match="unknown_method"):
        billing.subscribe(42, "solo", "https://otomata.tech/billing", method="wire")
    _wire_subscribe(monkeypatch, existing={"status": "active", "canceled_at": None,
                                           "customer_id": "cst_1", "plan": "solo"})
    with pytest.raises(ValueError, match="already_subscribed"):
        billing.subscribe(42, "solo", "https://otomata.tech/billing")


def test_subscribe_reuses_customer(monkeypatch):
    calls = _wire_subscribe(monkeypatch, existing={
        "status": "canceled", "canceled_at": "2026-07-01", "customer_id": "cst_old",
        "plan": "solo"})
    billing.subscribe(42, "solo", "https://otomata.tech/billing")
    assert "customer" not in calls                       # pas de re-création
    assert calls["payment"][1]["customer_id"] == "cst_old"


# ── confirm ──────────────────────────────────────────────────────────────────

def _wire_confirm(monkeypatch, *, payment, mandate=None, sub=None):
    state = {}
    row = {"id": 7, "kind": "initial", "status": "open",
           "payment_intent_id": "tr_1"}
    monkeypatch.setattr(db_billing, "get_org_subscription", lambda org: sub)
    monkeypatch.setattr(db_billing, "list_billing_payments", lambda org, limit=20: [row])
    monkeypatch.setattr(db_billing, "update_billing_payment",
                        lambda rid, **k: state.setdefault("update", (rid, k)) or True)
    monkeypatch.setattr(db_billing, "upsert_org_subscription",
                        lambda org, **k: state.setdefault("upsert", (org, k)))
    monkeypatch.setattr(billing.mollie_client, "get_payment", lambda i: payment)
    monkeypatch.setattr(billing.mollie_client, "valid_mandate", lambda cid: mandate)
    monkeypatch.setattr(billing, "apply_plan_entitlements", lambda org, plan: None)
    return state


def test_confirm_pending_while_on_checkout(monkeypatch):
    _wire_confirm(monkeypatch, payment={"status": "open"})
    assert billing.confirm(42)["status"] == "pending"


def test_confirm_failure_closes_payment(monkeypatch):
    state = _wire_confirm(monkeypatch, payment={"status": "failed"})
    assert billing.confirm(42)["status"] == "failed"
    assert state["update"][1]["status"] == "failed"
    assert "upsert" not in state                         # jamais de miroir sur échec


def test_confirm_success_opens_subscription(monkeypatch):
    state = _wire_confirm(
        monkeypatch,
        payment={"status": "paid", "customerId": "cst_1", "method": "creditcard",
                 "id": "tr_1", "metadata": {"org_id": "42", "plan": "solo"}},
        mandate={"id": "mdt_1", "mandateReference": "RUM123"})
    out = billing.confirm(42)
    assert out["status"] == "active" and out["plan"] == "solo" and out["method"] == "card"
    org, kw = state["upsert"]
    assert (org, kw["plan"], kw["mandate_id"], kw["status"]) == (42, "solo", "mdt_1", "active")
    assert kw["provider"] == "mollie"
    assert kw["next_billing_at"] == kw["current_period_end"]


def test_confirm_paid_without_mandate_refuses(monkeypatch):
    # encaissé mais aucun mandat valide → PAS d'abonnement irrenouvelable posé.
    _wire_confirm(monkeypatch,
                  payment={"status": "paid", "customerId": "cst_1",
                           "metadata": {"org_id": "42", "plan": "solo"}},
                  mandate=None)
    with pytest.raises(RuntimeError, match="no_mandate"):
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
