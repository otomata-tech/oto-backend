"""Gate d'acceptation légale (`me.legal`) — composition du statut + accept.

Stub de `db.get/record_legal_acceptances` (convention repo : pas de vrai PG en unit).
Le roundtrip SQL réel (upsert ON CONFLICT, forme de row) a été prouvé sur PG 16 —
ici on couvre la LOGIQUE : outstanding par contexte, accept, contexte inconnu.
"""
import pytest

from oto_mcp import db, legal_docs
from oto_mcp.capabilities import me_legal
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture
def store(monkeypatch):
    state: dict[str, dict] = {}

    def _get(sub):
        return dict(state)

    def _record(sub, items):
        for slug, version in items:
            state[slug] = {"version": version, "accepted_at": "2026-07-09 10:00:00"}

    monkeypatch.setattr(db, "get_legal_acceptances", _get)
    monkeypatch.setattr(db, "record_legal_acceptances", _record)
    return state


def _ctx():
    return ResolvedCtx(sub="s1", org_id=None, role="member")


def test_initial_all_outstanding(store):
    st = me_legal._status("s1")
    assert st["contexts"]["access"]["outstanding"] == ["terms"]
    assert st["contexts"]["purchase"]["outstanding"] == ["terms", "cgv", "dpa"]
    assert all(not d["accepted"] for d in st["documents"])


def test_accept_access_clears_access_only(store):
    st = me_legal._accept(_ctx(), me_legal.AcceptInput(context="access"))
    assert st["contexts"]["access"]["outstanding"] == []
    assert st["contexts"]["purchase"]["outstanding"] == ["cgv", "dpa"]
    terms = next(d for d in st["documents"] if d["slug"] == "terms")
    assert terms["accepted"] and terms["accepted_version"] == legal_docs.CURRENT_DOCS["terms"]["version"]


def test_accept_purchase_clears_all(store):
    me_legal._accept(_ctx(), me_legal.AcceptInput(context="purchase"))
    st = me_legal._status("s1")
    assert st["contexts"]["purchase"]["outstanding"] == []


def test_version_bump_reopens(store, monkeypatch):
    me_legal._accept(_ctx(), me_legal.AcceptInput(context="access"))
    monkeypatch.setitem(legal_docs.CURRENT_DOCS["terms"], "version", "999.0")
    st = me_legal._status("s1")
    assert st["contexts"]["access"]["outstanding"] == ["terms"]


def test_unknown_context_rejected(store):
    with pytest.raises(AuthzDenied) as e:
        me_legal._accept(_ctx(), me_legal.AcceptInput(context="bogus"))
    assert e.value.status == 400 and e.value.code == "unknown_context"
