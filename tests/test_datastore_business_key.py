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


def _fake_merge_locked(rows):
    """Stub de `db.datastore_merge_row_locked` (seam verrou de ligne #197) sur un
    dict `rows` en mémoire : reproduit get -> apply_fn -> update de façon
    séquentielle. Renvoie (row, merged) ou None si la row n'existe pas."""
    def merge_locked(ns_id, row_id, apply_fn, updated_at):
        if row_id not in rows:
            return None
        merged = apply_fn(dict(rows[row_id]))
        rows[row_id] = dict(merged)
        return ({"row_id": row_id, "created_at": "t0", "updated_at": updated_at,
                 "data": dict(merged)}, merged)
    return merge_locked


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
    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked",
                        _fake_merge_locked(state["rows"]))
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


# ── append unitaire : même dédup clé-métier que le batch (Sentry ds_bkey_109) ──

def test_append_row_existing_key_merges_not_500(monkeypatch):
    """Régression Sentry STARLETTE-3B : un append REST unitaire sur une clé métier
    DÉJÀ présente doit MERGER (comme le batch), pas remonter une UniqueViolation."""
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(st, "declared_key", lambda ns: "member_id")
    monkeypatch.setattr(dsm.db, "get_datastore_namespace_by_id",
                        lambda ns_id: {"id": ns_id, "schema": {"key": "member_id"}})
    rows = {"r1": {"member_id": "A", "x": 1}}
    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key",
                        lambda ns_id, key, kv: "r1" if str(kv) == "A" else None)
    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", _fake_merge_locked(rows))
    # insert ne doit JAMAIS être atteint (la clé existe) — le câbler à un raise le prouve
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("insert appelé")))

    out = st.append_row("t", {"member_id": "A", "y": 2})
    assert out["_id"] == "r1"
    assert rows["r1"] == {"member_id": "A", "x": 1, "y": 2}  # merge, pas d'écrasement


def test_append_row_lost_race_converges(monkeypatch):
    """Course : lookup ne voit rien, l'insert viole l'index, le 2e lookup trouve la
    gagnante → merge au lieu de 500."""
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(st, "declared_key", lambda ns: "member_id")
    monkeypatch.setattr(dsm.db, "get_datastore_namespace_by_id",
                        lambda ns_id: {"id": ns_id, "schema": {"key": "member_id"}})
    rows = {"winner": {"member_id": "A", "x": 1}}
    state = {"lookups": 0}
    def find(ns_id, key, kv):
        state["lookups"] += 1
        return "winner" if state["lookups"] > 1 and str(kv) == "A" else None
    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", find)
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data: (_ for _ in ()).throw(
                            UniqueViolation("duplicate key ds_bkey_7")))
    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", _fake_merge_locked(rows))

    out = st.append_row("t", {"member_id": "A", "y": 2})
    assert out["_id"] == "winner"
    assert rows["winner"] == {"member_id": "A", "x": 1, "y": 2}
