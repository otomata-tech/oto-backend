"""Billing B4 (ADR 0043) — l'abonnement d'org = 2e source du seam has_option.

Priorité des sources : comp user > comp org > plan de l'abonnement actif.
`subscription_plan_for_org` (store) ne rend un plan que si status=active OU
past_due en grace — la lecture ne décide jamais d'une fermeture."""
from __future__ import annotations

from oto_mcp import access


def _wire(monkeypatch, *, user_comp=False, org_comp=False, plan=None, org=7):
    monkeypatch.setattr(access.db, "has_option_comp",
                        lambda et, eid, opt: user_comp if et == "user" else org_comp)
    monkeypatch.setattr(access.db, "subscription_plan_for_org", lambda oid: plan)
    monkeypatch.setattr(access, "current_org", lambda sub: org)


def test_comp_still_wins_without_subscription(monkeypatch):
    _wire(monkeypatch, org_comp=True)
    assert access.has_option("u1", "unipile") is True


def test_subscription_plan_unlocks_its_options(monkeypatch):
    _wire(monkeypatch, plan="solo")
    assert access.has_option("u1", "unipile") is True      # dans PLANS['solo']
    assert access.has_option("u1", "autre_option") is False  # pas dans le plan


def test_no_source_no_option(monkeypatch):
    _wire(monkeypatch)
    assert access.has_option("u1", "unipile") is False


def test_no_org_short_circuits(monkeypatch):
    # sans org active, seule la source comp USER compte (jamais d'abonnement).
    called = {}
    monkeypatch.setattr(access.db, "has_option_comp", lambda et, eid, opt: False)
    monkeypatch.setattr(access.db, "subscription_plan_for_org",
                        lambda oid: called.update(oid=oid) or "solo")
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    assert access.has_option("u1", "unipile") is False
    assert called == {}                                     # jamais interrogé


def test_explicit_org_kwarg_reaches_subscription(monkeypatch):
    # fiche admin d'un tiers : l'org EXPLICITE est utilisée (pas current_org).
    _wire(monkeypatch, plan="solo")
    monkeypatch.setattr(access, "current_org",
                        lambda sub: (_ for _ in ()).throw(AssertionError("ne doit pas être lu")))
    assert access.has_option("tiers", "unipile", org=99) is True