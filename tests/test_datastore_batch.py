"""Batch write datastore + clé métier déclarée (feedback 2026-07-03).

Exerce `DatastorePg._write_rows_to_ns` / `write_rows` en monkeypatchant les seams db
(insert/find/get/update) — logique de lot pure, sans PG : append par défaut, upsert
(merge) quand une clé est en vigueur (param explicite ou `schema.key`).
"""
from __future__ import annotations

import pytest

from oto_mcp import datastore as D


class FakeDB:
    """Store en mémoire minimal calquant les fns db.* utilisées par le batch."""

    def __init__(self):
        self.rows = {}          # row_id -> data
        self._seq = 0

    def datastore_insert_row(self, ns_id, row_id, data, created_at=None, updated_at=None):
        self.rows[row_id] = dict(data)
        return {"row_id": row_id, "data": dict(data), "created_at": "t", "updated_at": "t"}

    def datastore_find_row_id_by_key(self, ns_id, key_field, key_value):
        for rid, data in self.rows.items():
            if str(data.get(key_field)) == str(key_value):
                return rid
        return None

    def datastore_get_row(self, ns_id, row_id):
        return {"row_id": row_id, "data": dict(self.rows.get(row_id, {}))} if row_id in self.rows else None

    def datastore_update_row(self, ns_id, row_id, data, updated_at):
        self.rows[row_id] = dict(data)
        return {"row_id": row_id, "data": dict(data), "created_at": "t", "updated_at": updated_at}


@pytest.fixture
def store(monkeypatch):
    fake = FakeDB()
    for name in ("datastore_insert_row", "datastore_find_row_id_by_key",
                 "datastore_get_row", "datastore_update_row"):
        monkeypatch.setattr(D.db, name, getattr(fake, name))
    # v2 (ADR 0046) : le batch lit le schéma du namespace (validation/lifecycle
    # opt-in) — None = soft, comportement 0016 inchangé pour ces tests.
    monkeypatch.setattr(D.db, "get_datastore_namespace_by_id",
                        lambda ns_id: {"id": ns_id, "schema": None})
    # _new_id déterministe (sinon Math.random-like) pour des ids stables dans le test.
    seq = {"n": 0}
    def next_id():
        seq["n"] += 1
        return f"r{seq['n']}"
    monkeypatch.setattr(D, "_new_id", next_id)
    s = D.DatastorePg("u1")
    s._fake = fake
    return s


def test_batch_appends_without_key(store):
    out = store._write_rows_to_ns(1, [{"a": 1}, {"a": 2}], key=None)
    assert out["inserted"] == 2 and out["updated"] == 0 and out["count"] == 2
    assert store._fake.rows == {"r1": {"a": 1}, "r2": {"a": 2}}


def test_batch_upserts_on_key(store):
    store._write_rows_to_ns(1, [{"email": "a@x", "n": "A"}], key="email")
    # même clé → merge (pas de doublon), champs mis à jour + nouveaux fusionnés
    out = store._write_rows_to_ns(1, [{"email": "a@x", "n": "A2", "extra": 9}], key="email")
    assert out["inserted"] == 0 and out["updated"] == 1
    assert list(store._fake.rows.values()) == [{"email": "a@x", "n": "A2", "extra": 9}]


def test_batch_mixed_keyed_and_unkeyed_rows(store):
    # rows sans valeur de clé sont appendées ; celles avec clé dédupliquent
    store._write_rows_to_ns(1, [{"email": "a@x", "v": 1}], key="email")
    out = store._write_rows_to_ns(1, [{"email": "a@x", "v": 2}, {"v": 3}], key="email")
    assert out["inserted"] == 1 and out["updated"] == 1
    assert store._fake.rows["r1"] == {"email": "a@x", "v": 2}   # upserté
    assert any(d == {"v": 3} for d in store._fake.rows.values())  # appendé


def test_write_rows_uses_declared_schema_key(store, monkeypatch):
    monkeypatch.setattr(store, "get_schema", lambda ns: {"fields": [], "key": "siren"})
    monkeypatch.setattr(store, "_resolve", lambda ns, write=False: 7)
    store.write_rows("boites", [{"siren": "123", "nom": "X"}])
    out = store.write_rows("boites", [{"siren": "123", "nom": "Y"}])
    assert out["key"] == "siren" and out["updated"] == 1 and out["inserted"] == 0


def test_explicit_key_overrides_schema(store, monkeypatch):
    monkeypatch.setattr(store, "get_schema", lambda ns: {"key": "siren"})
    monkeypatch.setattr(store, "_resolve", lambda ns, write=False: 7)
    out = store.write_rows("t", [{"email": "a@x"}], key="email")
    assert out["key"] == "email"


def test_meta_cols_stripped(store):
    store._write_rows_to_ns(1, [{"_id": "nope", "_created_at": "x", "a": 1}], key=None)
    assert store._fake.rows == {"r1": {"a": 1}}


def test_row_must_be_dict(store):
    with pytest.raises(ValueError):
        store._write_rows_to_ns(1, [["not", "a", "dict"]], key=None)


def test_declared_key_none_when_absent(store, monkeypatch):
    monkeypatch.setattr(store, "get_schema", lambda ns: {"fields": []})
    assert store.declared_key("t") is None
    monkeypatch.setattr(store, "get_schema", lambda ns: None)
    assert store.declared_key("t") is None
