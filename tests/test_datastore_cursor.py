"""Pagination keyset de `data_rows` (oto-backend#109 ch.2). Curseur opaque stable
sur `row_id` (uuid7). Seams db monkeypatchés — logique de page pure, sans PG."""
from __future__ import annotations

import pytest

from oto_mcp import datastore as D

# Jeu ordonné par row_id (uuid7 = ordre de création).
_ROWS = [{"row_id": f"r{i:02d}", "created_at": "t", "updated_at": "t", "data": {"n": i}}
         for i in range(1, 6)]  # r01..r05


def _fake_after(ns_id, *, after_row_id=None, limit=100, q=None, filters=None):
    items = list(_ROWS)
    for f in (filters or []):
        items = [r for r in items if str(r["data"].get(f["field"])) == str(f["value"])]
    if after_row_id:
        items = [r for r in items if r["row_id"] > after_row_id]
    return items[:limit]


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(D.db, "datastore_list_rows_after", _fake_after)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 1)
    return s


# ── curseur opaque ──

def test_cursor_roundtrip():
    assert D._decode_cursor(D._encode_cursor("r02")) == "r02"


def test_bad_cursor_raises():
    with pytest.raises(D.InvalidCursor):
        D._decode_cursor("!!not-base64!!")


# ── pagination keyset ──

def test_paginates_until_dry(store):
    p1 = store.cursor_rows("ns", limit=2)
    assert [r["n"] for r in p1["rows"]] == [1, 2]
    assert p1["next_cursor"] is not None            # page pleine ⇒ il reste

    p2 = store.cursor_rows("ns", limit=2, cursor=p1["next_cursor"])
    assert [r["n"] for r in p2["rows"]] == [3, 4]
    assert p2["next_cursor"] is not None

    p3 = store.cursor_rows("ns", limit=2, cursor=p2["next_cursor"])
    assert [r["n"] for r in p3["rows"]] == [5]
    assert p3["next_cursor"] is None                # page partielle ⇒ fin


def test_exact_multiple_terminates_with_empty_page(store):
    # 5 lignes, limit=5 : la 1re page est pleine → next_cursor non nul, la 2e est vide.
    p1 = store.cursor_rows("ns", limit=5)
    assert len(p1["rows"]) == 5 and p1["next_cursor"] is not None
    p2 = store.cursor_rows("ns", limit=5, cursor=p1["next_cursor"])
    assert p2["rows"] == [] and p2["next_cursor"] is None


def test_filter_pushed_to_sql(store):
    p = store.cursor_rows("ns", filter={"n": 3})
    assert [r["n"] for r in p["rows"]] == [3] and p["next_cursor"] is None
