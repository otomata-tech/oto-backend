"""Billing par org (ADR 0043) — schéma + client Mollie, canari inerte.

Logique pure + contrat du client (transport mocké). Le chemin SQL réel est
vérifié au boot (CREATE TABLE IF NOT EXISTS appliqué par _init), convention
du repo.
"""
from __future__ import annotations

import json

import httpx
import pytest

from oto_mcp import mollie_client
from oto_mcp.db import _schema
from oto_mcp.db import billing as billing_db


# ── schéma ───────────────────────────────────────────────────────────────────

def test_schema_declares_billing_tables():
    assert "CREATE TABLE IF NOT EXISTS org_subscriptions" in _schema._SCHEMA
    assert "CREATE TABLE IF NOT EXISTS billing_payments" in _schema._SCHEMA
    # file de réconciliation : l'index partiel doit exclure EXACTEMENT les
    # statuts terminaux du store (sinon des lignes mortes re-pollées à vie, ou
    # des lignes vivantes invisibles).
    for st in billing_db.TERMINAL_PAYMENT_STATUSES:
        assert f"'{st}'" in _schema._SCHEMA


def test_init_drops_legacy_table_before_schema():
    # la migration #82→0043 (drop de l'org_subscriptions Stripe) doit courir
    # AVANT l'application de _SCHEMA, sinon boot KO (vécu 2026-07-06).
    import inspect
    from oto_mcp.db import _init

    src = inspect.getsource(_init.init_db)
    assert src.index("_drop_legacy_org_subscriptions") < src.index("conn.execute(_SCHEMA)")


def test_db_surface_exposes_billing():
    # ré-export plat db.<fn> (convention __init__) — sans connexion DB.
    import oto_mcp.db as db

    for fn in ("get_org_subscription", "upsert_org_subscription",
               "due_subscriptions", "insert_billing_payment",
               "open_billing_payments", "subscription_plan_for_org"):
        assert callable(getattr(db, fn)), fn


def test_set_subscription_status_rejects_unknown():
    with pytest.raises(ValueError):
        billing_db.set_subscription_status(1, "on_hold")


# ── client Mollie ─────────────────────────────────────────────────────────────

def test_client_requires_key(monkeypatch):
    monkeypatch.delenv("MOLLIE_API_KEY", raising=False)
    monkeypatch.setattr(mollie_client, "_client", None)
    with pytest.raises(RuntimeError, match="MOLLIE_API_KEY"):
        mollie_client.ping()


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(base_url="https://api.mollie.com",
                        transport=httpx.MockTransport(handler))


def test_client_builds_bearer_auth(monkeypatch):
    # la clé part en Bearer — contrat Mollie.
    monkeypatch.setenv("MOLLIE_API_KEY", "test_abc")
    monkeypatch.setattr(mollie_client, "_client", None)
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(mollie_client.httpx, "Client", FakeClient)
    mollie_client._c()
    assert captured["headers"]["Authorization"] == "Bearer test_abc"
    assert captured["base_url"] == "https://api.mollie.com"


def test_ping_path_and_bearer(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"_embedded": {"methods": []}})

    monkeypatch.setattr(mollie_client, "_c", lambda: httpx.Client(
        base_url="https://api.mollie.com",
        headers={"Authorization": "Bearer test_abc"},
        transport=httpx.MockTransport(handler)))
    assert mollie_client.ping() is True
    assert seen["path"] == "/v2/methods"
    assert seen["auth"] == "Bearer test_abc"


def test_amount_field_decimal_and_uppercase():
    # centimes internes → montant Mollie (string 2 décimales + devise MAJUSCULE).
    assert mollie_client.amount_field(4900, "eur") == {"currency": "EUR", "value": "49.00"}
    assert mollie_client.amount_field(25000, "eur")["value"] == "250.00"


def test_create_first_payment_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(201, json={
            "id": "tr_1", "status": "open",
            "_links": {"checkout": {"href": "https://www.mollie.com/checkout/x"}}})

    monkeypatch.setattr(mollie_client, "_c", lambda: _mock_client(handler))
    out = mollie_client.create_first_payment(
        4900, customer_id="cst_1", redirect_url="https://otomata.tech/r",
        method="creditcard", metadata={"org_id": "42", "plan": "solo"})
    assert out["id"] == "tr_1"
    assert mollie_client.checkout_url(out) == "https://www.mollie.com/checkout/x"
    assert seen["path"] == "/v2/payments"
    assert seen["body"] == {
        "amount": {"currency": "EUR", "value": "49.00"},
        "customerId": "cst_1", "sequenceType": "first",
        "redirectUrl": "https://otomata.tech/r", "method": "creditcard",
        "metadata": {"org_id": "42", "plan": "solo"},
    }


def test_recurring_payment_passes_idempotency_header(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "tr_r1", "status": "pending"})

    monkeypatch.setattr(mollie_client, "_c", lambda: _mock_client(handler))
    out = mollie_client.create_recurring_payment(
        4900, customer_id="cst_1", mandate_id="mdt_1",
        idempotency_key="org42-2026-08-06-a1")
    assert out["status"] == "pending"
    assert seen["idem"] == "org42-2026-08-06-a1"
    assert seen["body"]["sequenceType"] == "recurring"
    assert seen["body"]["mandateId"] == "mdt_1"


def test_api_error_surfaces_detail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "The mandate is invalid"})

    monkeypatch.setattr(mollie_client, "_c", lambda: _mock_client(handler))
    with pytest.raises(mollie_client.MollieError, match="422.*mandate is invalid"):
        mollie_client.create_recurring_payment(100, customer_id="cst_1")


def test_valid_mandate_picks_first_valid(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"_embedded": {"mandates": [
            {"id": "mdt_pending", "status": "pending"},
            {"id": "mdt_ok", "status": "valid"},
        ]}})

    monkeypatch.setattr(mollie_client, "_c", lambda: _mock_client(handler))
    m = mollie_client.valid_mandate("cst_1")
    assert m["id"] == "mdt_ok"


def test_payment_terminal_statuses():
    # `pending`/`open` NE SONT PAS terminaux (checkout / SEPA en dénouement).
    assert mollie_client.payment_is_terminal("paid")
    assert mollie_client.payment_is_terminal("expired")
    assert not mollie_client.payment_is_terminal("pending")
    assert not mollie_client.payment_is_terminal("open")


def test_method_vocabulary_mapping():
    assert mollie_client.mollie_method("card") == "creditcard"
    assert mollie_client.mollie_method("sepa") == "directdebit"
    assert mollie_client.method_from_mollie("directdebit") == "sepa"
    assert mollie_client.method_from_mollie(None) == "card"      # défaut
