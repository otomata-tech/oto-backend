"""Datastore v2 (ADR 0046) — intégration STORE : validation à l'écriture, cycle
de vie (release auto en terminal) et file de travail. Seams db stubés (pattern
test_datastore_business_key) — le SQL claim (FOR UPDATE SKIP LOCKED) se valide
sur PG, ici on fige la logique du store : quels seams sont appelés, avec quoi,
et ce qui est refusé.
"""
import pytest

import oto_mcp.datastore as dsm
from oto_mcp.datastore import DatastorePg, RowValidationError


SCHEMA = {
    "strict": True,
    "fields": [
        {"key": "fact_id", "type": "text", "required": True},
        {"key": "status", "role": "status",
         "lifecycle": {"states": ["nouveau", "en_cours", "qualified"],
                       "transitions": {"nouveau": ["en_cours"],
                                       "en_cours": ["qualified"]}}},
        {"key": "qualification", "required_when": {"status": "qualified"}},
    ],
}


@pytest.fixture()
def store(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    calls = {"insert": [], "update": [], "release": [], "claim": []}
    monkeypatch.setattr(dsm.db, "get_datastore_namespace_by_id",
                        lambda ns_id: {"id": ns_id, "schema": SCHEMA})
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data, *a, **k: (
                            calls["insert"].append(data) or
                            {"row_id": rid, "created_at": "t", "updated_at": "t",
                             "data": data}))
    monkeypatch.setattr(dsm.db, "datastore_update_row",
                        lambda ns_id, rid, data, ts: (
                            calls["update"].append((rid, data)) or
                            {"row_id": rid, "created_at": "t", "updated_at": ts,
                             "data": data}))
    monkeypatch.setattr(dsm.db, "datastore_release_claim",
                        lambda ns_id, rid, worker: (
                            calls["release"].append((rid, worker)) or True))
    return st, calls


# ── validation à l'écriture ──────────────────────────────────────────────────

def test_append_refuses_invalid_row(store):
    st, calls = store
    with pytest.raises(RowValidationError, match="fact_id"):
        st.append_row("leads", {"status": "nouveau"})
    assert calls["insert"] == []          # rien écrit sur refus


def test_append_accepts_valid_row(store):
    st, calls = store
    out = st.append_row("leads", {"fact_id": "f1", "status": "nouveau"})
    assert calls["insert"] and out["fact_id"] == "f1"


def test_update_validates_merged_not_patch(store, monkeypatch):
    """Un patch partiel ne doit PAS échouer sur un requis déjà présent en base."""
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"fact_id": "f1",
                                                     "status": "nouveau"}})
    out = st.update_row("leads", "r1", {"status": "en_cours"})
    assert out["status"] == "en_cours"    # fact_id vient du mergé, pas du patch


def test_guard_rail_qualified_needs_deliverables(store, monkeypatch):
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"fact_id": "f1",
                                                     "status": "en_cours"}})
    with pytest.raises(RowValidationError, match="qualification"):
        st.update_row("leads", "r1", {"status": "qualified"})
    assert calls["update"] == []
    out = st.update_row("leads", "r1", {"status": "qualified",
                                        "qualification": "gros conso + toiture"})
    assert out["status"] == "qualified"


def test_forbidden_transition_refused(store, monkeypatch):
    st, _ = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"fact_id": "f1",
                                                     "status": "nouveau"}})
    with pytest.raises(RowValidationError, match="transition"):
        st.update_row("leads", "r1", {"status": "qualified",
                                      "qualification": "x"})


# ── cycle de vie → release auto du claim ─────────────────────────────────────

def test_terminal_status_releases_claim(store, monkeypatch):
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"fact_id": "f1",
                                                     "status": "en_cours"}})
    st.update_row("leads", "r1", {"status": "qualified", "qualification": "ok!"})
    assert calls["release"] == [("r1", None)]   # libération inconditionnelle


def test_non_terminal_status_keeps_claim(store, monkeypatch):
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"fact_id": "f1",
                                                     "status": "nouveau"}})
    st.update_row("leads", "r1", {"status": "en_cours"})
    assert calls["release"] == []


# ── file de travail ──────────────────────────────────────────────────────────

def test_claim_next_plumbs_filters_and_exposes_lease(store, monkeypatch):
    st, _ = store
    seen = {}
    monkeypatch.setattr(dsm.db, "datastore_claim_next",
                        lambda ns_id, *, worker, lease_seconds, filters: (
                            seen.update(ns_id=ns_id, worker=worker,
                                        lease=lease_seconds, filters=filters) or
                            {"row_id": "r9", "created_at": "t", "updated_at": "t",
                             "data": {"fact_id": "f9", "status": "nouveau"},
                             "claimed_by": worker, "claimed_until": "t+900"}))
    row = st.claim_next("leads", worker="w-13", filter={"status": "nouveau"},
                        lease_s=600)
    assert seen == {"ns_id": 7, "worker": "w-13", "lease": 600,
                    "filters": [{"field": "status", "op": "eq", "value": "nouveau"}]}
    assert row["_claimed_by"] == "w-13" and row["fact_id"] == "f9"


def test_claim_next_empty_queue_returns_none(store, monkeypatch):
    st, _ = store
    monkeypatch.setattr(dsm.db, "datastore_claim_next",
                        lambda ns_id, **k: None)
    assert st.claim_next("leads", worker="w") is None


def test_claim_requires_worker(store):
    st, _ = store
    with pytest.raises(ValueError, match="worker"):
        st.claim_next("leads", worker="  ")


def test_release_guarded_by_worker(store, monkeypatch):
    st, _ = store
    seen = {}
    monkeypatch.setattr(dsm.db, "datastore_release_claim",
                        lambda ns_id, rid, worker: (
                            seen.update(rid=rid, worker=worker) or False))
    assert st.release_claim("leads", "r1", worker="w-13") is False
    assert seen == {"rid": "r1", "worker": "w-13"}


# ── définition de schéma gardée à la pose ────────────────────────────────────

def test_set_schema_rejects_invalid_definition(store, monkeypatch):
    st, _ = store
    monkeypatch.setattr(dsm.db, "set_datastore_schema", lambda *a: None)
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups", lambda *a: [])
    with pytest.raises(ValueError, match="type inconnu"):
        st.set_schema("leads", {"fields": [{"key": "x", "type": "wat"}]})
