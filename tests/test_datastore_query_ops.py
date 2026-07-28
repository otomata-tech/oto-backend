"""Requêtage serveur de `data_rows` (feedback #279) : opérateurs de filtre,
recherche plein texte `q`, tri `order_by` + son régime de pagination.

Avant : `filter` n'acceptait qu'un exact-match `{col: val}` → répondre à « quel
post a une autrice prénommée Sylvie ? » sur 749 lignes obligeait à dumper tout le
namespace (2 pages × ~500 Ko) puis à retailler en local. La couche SQL portait déjà
`q`/ops/`order_by` (chemin dashboard) : ces tests verrouillent leur traduction
depuis la face MCP. Seams db monkeypatchés — logique pure, sans PG.
"""
from __future__ import annotations

import pytest

from oto_mcp import datastore as D


# ── traduction filter MCP → clauses SQL ──

def test_scalar_is_equality():
    assert D._mcp_filter_clauses({"statut": "won"}) == [
        {"field": "statut", "op": "eq", "value": "won"}]


def test_list_is_membership():
    assert D._mcp_filter_clauses({"statut": ["won", "lost"]}) == [
        {"field": "statut", "op": "in", "value": ["won", "lost"]}]


def test_dict_is_an_explicit_operator():
    assert D._mcp_filter_clauses({"author_name": {"contains": "sylvie"}}) == [
        {"field": "author_name", "op": "contains", "value": "sylvie"}]


def test_operators_combine_across_and_within_columns():
    clauses = D._mcp_filter_clauses({
        "posted_at": {"gte": "2026-06-01", "lt": "2026-07-01"},
        "statut": "open",
    })
    assert {"field": "posted_at", "op": "gte", "value": "2026-06-01"} in clauses
    assert {"field": "posted_at", "op": "lt", "value": "2026-07-01"} in clauses
    assert {"field": "statut", "op": "eq", "value": "open"} in clauses


def test_unknown_operator_raises_rather_than_filtering_something_else():
    with pytest.raises(ValueError) as e:
        D._mcp_filter_clauses({"author_name": {"regex": "^syl"}})
    assert "regex" in str(e.value)          # nomme l'op fautif
    assert "contains" in str(e.value)       # et liste les ops acceptés


def test_no_filter_is_no_clause():
    assert D._mcp_filter_clauses(None) == []
    assert D._mcp_filter_clauses({}) == []


def test_every_declared_op_is_accepted():
    for op in D.db._DS_FILTER_OPS:
        assert D._mcp_filter_clauses({"c": {op: "v"}})[0]["op"] == op


# ── passe-plat vers le SQL : q, filtres, tri ──

_ROWS = [{"row_id": f"r{i:02d}", "created_at": "t", "updated_at": "t", "data": {"n": i}}
         for i in range(1, 6)]


@pytest.fixture
def spy(monkeypatch):
    """Store dont les deux chemins SQL enregistrent leurs kwargs."""
    calls: dict = {}

    def _after(ns_id, **kw):
        calls["after"] = kw
        return _ROWS[: kw.get("limit", 100)]

    def _list(ns_id, **kw):
        calls["list"] = kw
        offset = kw.get("offset", 0)
        return _ROWS[offset: offset + (kw.get("limit") or 100)]

    def _count(ns_id, q=None, filters=None):
        calls["count"] = {"q": q, "filters": filters}
        return 42

    monkeypatch.setattr(D.db, "datastore_list_rows_after", _after)
    monkeypatch.setattr(D.db, "datastore_list_rows", _list)
    monkeypatch.setattr(D.db, "datastore_count_rows", _count)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 1)
    return s, calls


def test_q_and_ops_reach_the_keyset_path(spy):
    store, calls = spy
    store.cursor_rows("ns", q="sylvie", filter={"posted_at": {"gte": "2026-06-01"}})
    assert calls["after"]["q"] == "sylvie"
    assert calls["after"]["filters"] == [
        {"field": "posted_at", "op": "gte", "value": "2026-06-01"}]


def test_count_only_shares_the_same_narrowing(spy):
    """Le total doit compter le MÊME jeu que la page (sinon « 0 ligne » trompeur)."""
    store, calls = spy
    assert store.count_rows("ns", q="sylvie", filter={"statut": "won"}) == 42
    assert calls["count"] == {
        "q": "sylvie", "filters": [{"field": "statut", "op": "eq", "value": "won"}]}


def test_order_by_switches_to_the_sorted_path(spy):
    store, calls = spy
    store.cursor_rows("ns", order_by="posted_at", order_dir="asc", limit=2)
    assert "after" not in calls                      # pas le keyset
    assert calls["list"]["order_by"] == "posted_at"
    assert calls["list"]["order_dir"] == "asc"
    assert calls["list"]["offset"] == 0


def test_sorted_pagination_advances_by_offset(spy):
    store, _ = spy
    p1 = store.cursor_rows("ns", order_by="n", limit=2)
    assert [r["n"] for r in p1["rows"]] == [1, 2]
    p2 = store.cursor_rows("ns", order_by="n", limit=2, cursor=p1["next_cursor"])
    assert [r["n"] for r in p2["rows"]] == [3, 4]
    p3 = store.cursor_rows("ns", order_by="n", limit=2, cursor=p2["next_cursor"])
    assert [r["n"] for r in p3["rows"]] == [5]
    assert p3["next_cursor"] is None                 # page partielle ⇒ fin


# ── les deux régimes de curseur ne se mélangent pas ──

def test_keyset_cursor_rejected_on_a_sorted_call(spy):
    store, _ = spy
    keyset = D._encode_cursor("r02")
    with pytest.raises(D.InvalidCursor):
        store.cursor_rows("ns", order_by="n", cursor=keyset)


def test_sorted_cursor_rejected_without_order_by(spy):
    store, _ = spy
    sorted_cursor = D._encode_offset_cursor(4)
    with pytest.raises(D.InvalidCursor):
        store.cursor_rows("ns", cursor=sorted_cursor)


def test_offset_cursor_roundtrip():
    assert D._decode_offset_cursor(D._encode_offset_cursor(12)) == 12


def test_garbled_offset_cursor_raises():
    with pytest.raises(D.InvalidCursor):
        D._decode_offset_cursor(D._encode_cursor("off:notanumber"))
