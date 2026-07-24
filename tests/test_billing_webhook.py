"""Billing (ADR 0043) — webhook Mollie : réconciliation événementielle.

Logique domaine `process_webhook` (store + Mollie monkeypatchés) + montage de la
route publique non authentifiée."""
from __future__ import annotations

from oto_mcp import billing
from oto_mcp.db import billing as db_billing


def _wire(monkeypatch, *, row, payment=None, confirm_spy=None):
    monkeypatch.setattr(db_billing, "get_billing_payment_by_ref", lambda ref: row)
    monkeypatch.setattr(billing.mollie_client, "get_payment", lambda pid: payment or {})
    updates = []
    monkeypatch.setattr(db_billing, "update_billing_payment",
                        lambda rid, **k: updates.append((rid, k)) or True)
    spy = confirm_spy if confirm_spy is not None else {}
    monkeypatch.setattr(billing, "confirm", lambda org: spy.__setitem__("org", org))
    return updates


def test_webhook_ignores_unknown_id(monkeypatch):
    # un id forgé / hors journal ne déclenche RIEN (pas même un fetch Mollie).
    called = {}
    monkeypatch.setattr(db_billing, "get_billing_payment_by_ref", lambda ref: None)
    monkeypatch.setattr(billing.mollie_client, "get_payment",
                        lambda pid: called.setdefault("fetched", True))
    assert billing.process_webhook("tr_forged") == "ignored"
    assert "fetched" not in called


def test_webhook_paid_initial_replays_confirm(monkeypatch):
    spy = {}
    _wire(monkeypatch,
          row={"id": 1, "kind": "initial", "org_id": 42, "status": "open"},
          payment={"status": "paid"}, confirm_spy=spy)
    assert billing.process_webhook("tr_1") == "confirmed"
    assert spy["org"] == 42                       # miroir posé (idempotent)


def test_webhook_updates_changed_status(monkeypatch):
    updates = _wire(monkeypatch,
                    row={"id": 9, "kind": "renewal", "org_id": 42, "status": "pending"},
                    payment={"status": "failed"})
    assert billing.process_webhook("tr_r1") == "updated"
    assert updates == [(9, {"status": "failed"})]


def test_webhook_noop_when_status_unchanged(monkeypatch):
    updates = _wire(monkeypatch,
                    row={"id": 9, "kind": "renewal", "org_id": 42, "status": "paid"},
                    payment={"status": "paid"})
    assert billing.process_webhook("tr_r1") == "unchanged"
    assert updates == []


def test_webhook_url_uses_public_base(monkeypatch):
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    assert billing.webhook_url() == "https://mcp.oto.cx/api/billing/webhook"


def test_webhook_route_registered_public():
    from oto_mcp import api_routes_billing

    routes = api_routes_billing.make_routes(lambda req: None)
    paths = {r.path for r in routes}
    assert "/api/billing/webhook" in paths
    post = [r for r in routes if "POST" in r.methods]
    assert len(post) == 1                          # la route webhook est bien un POST
