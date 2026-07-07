"""Acceptation des documents légaux (CGU/CGV/DPA) + gate d'achat.

Le store est monkeypatché (pas de PG) : on teste la LOGIQUE — calcul du
reste-à-accepter, enregistrement, et le gate `billing.subscribe`.
"""
from __future__ import annotations

import pytest

from oto_mcp import legal_docs
from oto_mcp.db import legal_acceptance as dbla
from oto_mcp.capabilities import legal as cap_legal
from oto_mcp.capabilities import billing as cap_billing
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _ctx(sub="u1", org_id=42):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _wire(monkeypatch, latest=None):
    """Stub le store : `latest` = état des dernières acceptations ; capture les writes."""
    recorded = []
    monkeypatch.setattr(dbla, "latest_acceptances", lambda sub, org_id=None: dict(latest or {}))
    monkeypatch.setattr(
        dbla, "record_legal_acceptance",
        lambda sub, slug, version, context, **kw: recorded.append(
            (sub, slug, version, context, kw.get("org_id"))))
    return recorded


# ── reste-à-accepter ───────────────────────────────────────────────────────────

def test_outstanding_all_when_nothing_accepted():
    out = cap_legal.outstanding_slugs({}, "purchase")
    assert out == list(legal_docs.required_slugs("purchase"))  # terms, cgv, dpa


def test_outstanding_stale_version_still_required():
    # terms accepté mais à une VIEILLE version → reste requis (re-sollicitation).
    latest = {
        "terms": {"version": "1.0"},
        "cgv": {"version": legal_docs.CURRENT_VERSIONS["cgv"]},
        "dpa": {"version": legal_docs.CURRENT_VERSIONS["dpa"]},
    }
    assert cap_legal.outstanding_slugs(latest, "purchase") == ["terms"]


def test_outstanding_empty_when_all_current():
    latest = {s: {"version": legal_docs.CURRENT_VERSIONS[s]}
              for s in legal_docs.required_slugs("purchase")}
    assert cap_legal.outstanding_slugs(latest, "purchase") == []


# ── status / accept ─────────────────────────────────────────────────────────────

def test_status_marks_accepted_at_current_version(monkeypatch):
    _wire(monkeypatch, latest={"terms": {"version": legal_docs.CURRENT_VERSIONS["terms"],
                                          "context": "access", "org_id": None,
                                          "accepted_at": "2026-07-07 10:00:00"}})
    payload = cap_legal._status(_ctx(), cap_legal.NoInput())
    by_slug = {d["slug"]: d for d in payload["documents"]}
    assert by_slug["terms"]["accepted"] is True
    assert by_slug["cgv"]["accepted"] is False
    assert by_slug["cgv"]["url"].endswith("/cgv")
    assert payload["contexts"]["purchase"]["outstanding"] == ["cgv", "dpa"]


def test_accept_records_required_docs_of_context(monkeypatch):
    recorded = _wire(monkeypatch, latest={})
    cap_legal._accept(_ctx(), cap_legal.AcceptInput(context="purchase"))
    slugs = {r[1] for r in recorded}
    assert slugs == set(legal_docs.required_slugs("purchase"))
    # versions courantes + contexte + org scope
    assert all(r[3] == "purchase" and r[4] == 42 for r in recorded)
    assert all(r[2] == legal_docs.CURRENT_VERSIONS[r[1]] for r in recorded)


def test_accept_rejects_unknown_document(monkeypatch):
    _wire(monkeypatch, latest={})
    with pytest.raises(AuthzDenied) as e:
        cap_legal._accept(_ctx(), cap_legal.AcceptInput(context="x", slugs=["nope"]))
    assert e.value.code == "unknown_document"


# ── gate d'achat (billing.subscribe) ────────────────────────────────────────────

def test_purchase_gate_blocks_when_not_accepted(monkeypatch):
    _wire(monkeypatch, latest={})
    with pytest.raises(AuthzDenied) as e:
        cap_billing._require_legal_acceptance(_ctx(), accept_now=False)
    assert e.value.code == "terms_not_accepted"


def test_purchase_gate_records_when_accept_now(monkeypatch):
    recorded = _wire(monkeypatch, latest={})
    cap_billing._require_legal_acceptance(_ctx(), accept_now=True)
    assert {r[1] for r in recorded} == set(legal_docs.required_slugs("purchase"))
    assert all(r[3] == "purchase" and r[4] == 42 for r in recorded)


def test_purchase_gate_noop_when_already_current(monkeypatch):
    latest = {s: {"version": legal_docs.CURRENT_VERSIONS[s]}
              for s in legal_docs.required_slugs("purchase")}
    recorded = _wire(monkeypatch, latest=latest)
    cap_billing._require_legal_acceptance(_ctx(), accept_now=False)  # ne lève pas
    assert recorded == []
