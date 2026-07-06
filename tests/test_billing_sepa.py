"""Billing phase 2 (ADR 0043) — voie PRÉLÈVEMENT : IBAN → mandat (sign_url,
OTP SMS) → miroir `incomplete` → confirm polle le mandat → 1er SDD + activation
→ le runner rejoue sur sepa_id. Chemin sandbox réel exercé le 2026-07-06
(sepa_xxx + mndt_xxx + sign_url ; « no valid mandate » tant que non signé)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import billing, billing_runner
from oto_mcp.db import billing as db_billing

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


# ── subscribe (voie sepa) ────────────────────────────────────────────────────

def _wire_sepa_subscribe(monkeypatch):
    state = {}
    monkeypatch.setattr(db_billing, "get_org_subscription", lambda org: None)
    monkeypatch.setattr(db_billing, "upsert_org_subscription",
                        lambda org, **k: state.update(upsert=(org, k)))
    monkeypatch.setattr(billing.stancer_client, "_req",
                        lambda m, p, json=None: {"id": "cust_s1"})
    monkeypatch.setattr(billing.stancer_client, "create_sepa",
                        lambda **k: state.update(sepa=k) or {"id": "sepa_1"})
    monkeypatch.setattr(billing.stancer_client, "create_mandate",
                        lambda sid: {"id": "mndt_1",
                                     "sign_url": "https://mandate.stancer.com/test_mndt_1/sign"})
    return state


def test_sepa_subscribe_returns_sign_url_and_incomplete_mirror(monkeypatch):
    state = _wire_sepa_subscribe(monkeypatch)
    out = billing.subscribe(42, "standard", "https://oto.cx/billing",
                            method="sepa", iban="FR14…", holder_name="ACME SAS",
                            mobile="+33612345678")
    assert out["checkout_url"].endswith("/sign")
    assert out["method"] == "sepa"
    org, kw = state["upsert"]
    assert (org, kw["method"], kw["status"]) == (42, "sepa", "incomplete")
    assert kw["sepa_id"] == "sepa_1" and kw["mandate_id"] == "mndt_1"


def test_sepa_subscribe_requires_all_fields(monkeypatch):
    _wire_sepa_subscribe(monkeypatch)
    with pytest.raises(ValueError, match="sepa_fields_required"):
        billing.subscribe(42, "standard", "https://oto.cx/billing",
                          method="sepa", iban="FR14…")  # mobile/holder manquants


def test_unknown_method_rejected(monkeypatch):
    _wire_sepa_subscribe(monkeypatch)
    with pytest.raises(ValueError, match="unknown_method"):
        billing.subscribe(42, "standard", "https://oto.cx/billing", method="wire")


# ── confirm (voie sepa) ──────────────────────────────────────────────────────

_SUB_INCOMPLETE = {"org_id": 42, "plan": "standard", "method": "sepa",
                   "status": "incomplete", "sepa_id": "sepa_1",
                   "mandate_id": "mndt_12345678", "customer_id": "cust_s1"}


def _wire_sepa_confirm(monkeypatch, *, mandate):
    state = {}
    monkeypatch.setattr(db_billing, "get_org_subscription",
                        lambda org: dict(_SUB_INCOMPLETE))
    monkeypatch.setattr(db_billing, "insert_billing_payment",
                        lambda *a, **k: state.setdefault("journal", (a, k)) or 9)
    monkeypatch.setattr(db_billing, "update_billing_payment",
                        lambda rid, **k: state.update(update=(rid, k)) or True)
    monkeypatch.setattr(db_billing, "activate_subscription",
                        lambda org, **k: state.update(activate=(org, k)) or True)
    monkeypatch.setattr(billing.stancer_client, "get_mandate", lambda mid: mandate)
    monkeypatch.setattr(billing.stancer_client, "create_payment",
                        lambda amount, **k: state.update(charge=(amount, k)) or {
                            "id": "paym_s1", "status": "to_capture"})
    return state


def test_confirm_sepa_pending_until_signed(monkeypatch):
    state = _wire_sepa_confirm(monkeypatch, mandate={
        "signed_at": None, "sign_url": "https://mandate.stancer.com/x/sign"})
    out = billing.confirm(42)
    assert out["status"] == "pending"
    assert out["sign_url"].endswith("/sign")     # ré-exposée au dashboard
    assert "charge" not in state                 # AUCUN débit sans signature


def test_confirm_sepa_signed_charges_and_activates(monkeypatch):
    state = _wire_sepa_confirm(monkeypatch, mandate={
        "signed_at": 1783333373, "rum": "RUM123"})
    out = billing.confirm(42)
    assert out["status"] == "active" and out["method"] == "sepa"
    amount, kw = state["charge"]
    assert amount == billing.PLANS["standard"]["amount"]
    assert kw["sepa"] == "sepa_1"
    # unique_id ancré sur le mandat : double confirm → 409, re-souscription unique
    assert kw["unique_id"] == "org42-init-12345678"
    org, akw = state["activate"]
    assert org == 42 and akw["mandate_rum"] == "RUM123"


# ── runner (échéances sepa) ──────────────────────────────────────────────────

def test_runner_charges_sepa_token(monkeypatch):
    state = {}
    monkeypatch.setattr(db_billing, "count_renewal_attempts", lambda org, since: 0)
    monkeypatch.setattr(db_billing, "insert_billing_payment", lambda *a, **k: 3)
    monkeypatch.setattr(db_billing, "update_billing_payment", lambda rid, **k: True)
    monkeypatch.setattr(db_billing, "schedule_next_billing",
                        lambda org, pe, nb: state.update(schedule=(org, pe)) or True)
    monkeypatch.setattr(billing_runner.stancer_client, "create_payment",
                        lambda amount, **k: state.update(charge=k) or {
                            "id": "paym_r1", "status": "to_capture"})
    sub = {"org_id": 42, "plan": "standard", "method": "sepa",
           "sepa_id": "sepa_1", "card_id": None, "customer_id": "cust_s1",
           "current_period_end": NOW - timedelta(hours=1), "status": "active"}
    assert billing_runner._charge_one(sub, NOW) == "renewed"
    assert state["charge"]["sepa"] == "sepa_1"
    assert state["charge"]["card"] is None


def test_runner_skips_sepa_without_token(monkeypatch):
    sub = {"org_id": 42, "plan": "standard", "method": "sepa", "sepa_id": None,
           "current_period_end": NOW, "status": "active"}
    assert billing_runner._charge_one(sub, NOW) == "skipped"
