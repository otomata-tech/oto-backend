"""Billing par org (ADR 0043, B1) — schéma + client Stancer, canari inerte.

Logique pure + contrat du client (transport mocké). Le chemin SQL réel est
vérifié au boot (CREATE TABLE IF NOT EXISTS appliqué par _init), convention
du repo.
"""
from __future__ import annotations

import base64

import httpx
import pytest

from oto_mcp import stancer_client
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


# ── client Stancer ───────────────────────────────────────────────────────────

def test_client_requires_key(monkeypatch):
    monkeypatch.delenv("STANCER_API_KEY", raising=False)
    monkeypatch.setattr(stancer_client, "_client", None)
    with pytest.raises(RuntimeError, match="STANCER_API_KEY"):
        stancer_client.ping()


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(base_url="https://api.stancer.com",
                        transport=httpx.MockTransport(handler))


def test_client_builds_basic_auth(monkeypatch):
    # la clé part en HTTP Basic username (password vide) — contrat Stancer.
    monkeypatch.setenv("STANCER_API_KEY", "stest_abc")
    monkeypatch.setattr(stancer_client, "_client", None)
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(stancer_client.httpx, "Client", FakeClient)
    stancer_client._c()
    assert captured["auth"] == ("stest_abc", "")
    assert captured["base_url"] == "https://api.stancer.com"


def test_ping_path(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"pong": True})

    monkeypatch.setattr(stancer_client, "_c", lambda: httpx.Client(
        base_url="https://api.stancer.com", auth=("stest_abc", ""),
        transport=httpx.MockTransport(handler)))
    assert stancer_client.ping() is True
    assert seen["path"] == "/v2/ping"
    # format wire du Basic : base64("clé:")
    assert seen["auth"] == "Basic " + base64.b64encode(b"stest_abc:").decode()


def test_create_payment_intent_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": "pi_1", "status": "require_payment_method",
                                         "url": "https://payment.stancer.com/…"})

    monkeypatch.setattr(stancer_client, "_c", lambda: _mock_client(handler))
    out = stancer_client.create_payment_intent(
        4900, customer="cust_1", return_url="https://oto.cx/r", order_id="org-42")
    assert out["id"] == "pi_1"
    assert seen["path"] == "/v2/payment_intents/"
    assert seen["body"] == {
        "amount": 4900, "currency": "eur", "methods_allowed": ["card"],
        "customer": "cust_1", "return_url": "https://oto.cx/r", "order_id": "org-42",
    }


def test_create_payment_requires_token():
    with pytest.raises(ValueError, match="card.*sepa|sepa.*card"):
        stancer_client.create_payment(4900)


def test_api_error_surfaces_detail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "card declined"})

    monkeypatch.setattr(stancer_client, "_c", lambda: _mock_client(handler))
    with pytest.raises(stancer_client.StancerError, match="402.*card declined"):
        stancer_client.create_payment(100, card="card_1", unique_id="u1")


def test_intent_terminal_statuses():
    # `authorized` n'est PAS terminal (capture attendue) ; polling continue.
    assert stancer_client.intent_is_terminal("captured")
    assert stancer_client.intent_is_terminal("unpaid")
    assert not stancer_client.intent_is_terminal("authorized")
    assert not stancer_client.intent_is_terminal("processing")
