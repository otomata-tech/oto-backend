"""Clé métier = contrainte (#109 ch.3) — cycle de vie + convergence sous course.

Le SQL (index d'expression UNIQUE partiel, merge des doublons, plan indexé) a été
validé empiriquement sur PG 16 (2026-07-06). Ici : la logique du STORE, seams db
stubés — (1) set_schema refuse une clé sur données sales, pose/dépose l'index ;
(2) le batch write convertit une UniqueViolation (course perdue) en update-merge,
et relève franchement une violation inexpliquée.
"""
import pytest
from psycopg.errors import UniqueViolation

import oto_mcp.datastore as dsm
from oto_mcp.datastore import DatastorePg


@pytest.fixture()
def store(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    calls = {"ensure": [], "drop": [], "set": []}
    monkeypatch.setattr(dsm.db, "set_datastore_schema",
                        lambda ns_id, schema: calls["set"].append((ns_id, schema)))
    monkeypatch.setattr(dsm.db, "datastore_ensure_key_index",
                        lambda ns_id, key: calls["ensure"].append((ns_id, key)))
    monkeypatch.setattr(dsm.db, "datastore_drop_key_index",
                        lambda ns_id: calls["drop"].append(ns_id))
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups", lambda ns_id, key: [])
    return st, calls


# ── set_schema : cycle de vie de la contrainte ───────────────────────────────

def test_set_schema_with_key_ensures_index(store):
    st, calls = store
    st.set_schema("t", {"key": "member_id", "fields": []})
    assert calls["ensure"] == [(7, "member_id")] and calls["drop"] == []


def test_set_schema_without_key_drops_index(store):
    st, calls = store
    st.set_schema("t", {"fields": []})
    assert calls["drop"] == [7] and calls["ensure"] == []


def test_set_schema_clear_drops_index(store):
    st, calls = store
    st.set_schema("t", None)
    assert calls["drop"] == [7]


def test_set_schema_refuses_key_on_dirty_data(store, monkeypatch):
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups",
                        lambda ns_id, key: [{"value": "m-42", "n": 3}])
    with pytest.raises(ValueError, match="DOUBLON"):
        st.set_schema("t", {"key": "member_id"})
    assert calls["set"] == [] and calls["ensure"] == []   # rien persisté sur refus


# ── batch write : convergence sous course (UniqueViolation → update) ─────────

@pytest.fixture()
def race(monkeypatch):
    """Simule la course : le lookup ne voit rien, l'insert viole (un write
    concurrent a gagné), la ligne gagnante devient trouvable au 2e lookup."""
    st = DatastorePg("u", acting_org=35)
    state = {"rows": {"winner": {"member_id": "A", "x": 1}}, "lookups": 0}

    def find(ns_id, key, kv):
        state["lookups"] += 1
        return "winner" if state["lookups"] > 1 and str(kv) == "A" else None

    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", find)
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data: (_ for _ in ()).throw(
                            UniqueViolation("duplicate key ds_bkey_7")))
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "data": dict(state["rows"][rid])})
    monkeypatch.setattr(dsm.db, "datastore_update_row",
                        lambda ns_id, rid, data, ts: state["rows"].__setitem__(rid, data))
    monkeypatch.setattr(dsm.db, "get_datastore_namespace_by_id",
                        lambda ns_id: {"id": ns_id, "schema": {"key": "member_id"}})
    return st, state


def test_lost_race_converges_to_update(race):
    st, state = race
    out = st._write_rows_to_ns(7, [{"member_id": "A", "y": 2}], key="member_id")
    assert out == {"inserted": 0, "updated": 1, "count": 1,
                   "key": "member_id", "ids": ["winner"]}
    assert state["rows"]["winner"] == {"member_id": "A", "x": 1, "y": 2}  # merge


def test_unexplained_violation_raises(race, monkeypatch):
    # La violation ne s'explique pas par la clé déclarée (row sans cette clé) →
    # erreur FRANCHE, jamais un repli muet.
    st, _ = race
    with pytest.raises(UniqueViolation):
        st._write_rows_to_ns(7, [{"autre": "champ"}], key=None)
