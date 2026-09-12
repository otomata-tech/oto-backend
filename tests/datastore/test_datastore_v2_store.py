"""Datastore v2 (ADR 0046) — intégration STORE : validation à l'écriture, cycle
de vie (release auto en terminal) et file de travail. Seams db stubés (pattern
test_datastore_business_key) — le SQL claim (FOR UPDATE SKIP LOCKED) se valide
sur PG, ici on fige la logique du store : quels seams sont appelés, avec quoi,
et ce qui est refusé.
"""
import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore.core import DatastorePg, RowValidationError


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
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": SCHEMA})
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data, *a, **k: (
                            calls["insert"].append(data) or
                            {"row_id": rid, "created_at": "t", "updated_at": "t",
                             "data": data}))
    def fusion(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : elle lit
        # la ligne que le test a posée (`datastore_get_row`, résolu à l'appel), et la
        # fusion aboutie est relevée comme l'était l'UPDATE.
        ligne = dsm.db.datastore_get_row(ns_id, rid)
        if ligne is None:
            return None
        data = apply_fn(dict(ligne.get("data") or {}))
        calls["update"].append((rid, data))
        return ({"row_id": rid, "created_at": "t", "updated_at": updated_at,
                 "data": data}, data)

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion)
    monkeypatch.setattr(dsm.db, "datastore_release_claim",
                        lambda ns_id, rid, worker: (
                            calls["release"].append((rid, worker)) or True))
    # #317 : la protection en écriture lit le bail ACTIF avant les gestes qui
    # n'ouvrent pas de verrou de ligne. Ces tests portent sur la validation de
    # schéma, pas sur le verrou — la ligne y est libre.
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
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

def test_terminal_status_no_longer_releases_claim(store, monkeypatch):
    """⚠️ Ce test gardait le comportement que #317 RETIRE — il est retourné, pas
    supprimé : c'est lui qui garantit désormais que le verrou ne dépend plus de ce
    que le client appelle « terminé ».

    La raison du retrait : pour savoir qu'un travail était fini, la plateforme devait
    connaître les états du client et lesquels sont des fins. Une mission a payé ce
    couplage — deux champs d'état, le verrou écoutait celui que personne ne
    remplissait, et le mécanisme n'a jamais fonctionné. La fin de travail est
    désormais un acte du verrou (fin de traitement, ou release explicite)."""
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"fact_id": "f1",
                                                     "status": "en_cours"}})
    st.update_row("leads", "r1", {"status": "qualified", "qualification": "ok!"})
    assert calls["release"] == []


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
                        lambda ns_id, *, worker, lease_seconds, filters, **k: (
                            seen.update(ns_id=ns_id, worker=worker,
                                        lease=lease_seconds, filters=filters) or
                            {"row_id": "r9", "created_at": "t", "updated_at": "t",
                             "data": {"fact_id": "f9", "status": "nouveau"},
                             "claimed_by": worker, "claimed_until": "t+900",
                             "claimed_run": None, "claim_active": True}))
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
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    issue = st.release_claim("leads", "r1", worker="w-13")
    assert (issue["released"], issue["reason"]) == (False, "no_lease")
    assert seen == {"rid": "r1", "worker": "w-13"}


# ── définition de schéma gardée à la pose ────────────────────────────────────

def test_set_schema_rejects_invalid_definition(store, monkeypatch):
    st, _ = store
    monkeypatch.setattr(dsm.db, "set_datastore_schema", lambda *a: None)
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups", lambda *a: [])
    with pytest.raises(ValueError, match="type inconnu"):
        st.set_schema("leads", {"fields": [{"key": "x", "type": "wat"}]})


# ── borne de longueur (#383) ─────────────────────────────────────────────────

BOUNDED = {"fields": [{"key": "fonction", "type": "text", "max_length": 60},
                      {"key": "notes", "type": "text"}]}


@pytest.fixture()
def bounded(store, monkeypatch):
    st, calls = store
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": BOUNDED})
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {
                            "row_id": rid, "created_at": "t", "updated_at": "t",
                            "data": {"fonction": "x" * 247, "notes": ""}})
    return st, calls


def test_bound_refuses_the_overlong_write(bounded):
    st, calls = bounded
    with pytest.raises(RowValidationError, match="247 caractères, maximum 60"):
        st.append_row("viviers", {"fonction": "x" * 247})
    assert calls["insert"] == []


def test_patch_of_another_field_survives_an_overlong_row_in_place(bounded):
    """La ligne en base dépasse déjà : patcher `notes` doit passer, patcher
    `fonction` non. Sinon poser une borne gèlerait tout l'historique (#383)."""
    st, calls = bounded
    out = st.update_row("viviers", "r1", {"notes": "rappelé le 12"})
    assert out["notes"] == "rappelé le 12" and calls["update"]
    with pytest.raises(RowValidationError, match="maximum 60"):
        st.update_row("viviers", "r1", {"fonction": "y" * 90})


def test_set_schema_warns_about_rows_already_over_the_bound(store, monkeypatch):
    st, _ = store
    monkeypatch.setattr(dsm.db, "set_datastore_schema", lambda *a: None)
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups", lambda *a: [])
    # oto#82 : ce db stubbé ne porte aucun index — la pose reste donc faite.
    monkeypatch.setattr(dsm.db, "datastore_has_key_index", lambda ns_id: False)
    monkeypatch.setattr(dsm.db, "datastore_drop_key_index", lambda *a: None)
    seen = {}
    monkeypatch.setattr(dsm.db, "datastore_overlong_fields",
                        lambda ns_id, bounds: (
                            seen.update(ns_id=ns_id, bounds=bounds) or
                            [{"field": "fonction", "max_length": 60,
                              "rows": 12, "longest": 247}]))
    out = st.set_schema("viviers", BOUNDED)
    assert seen == {"ns_id": 7, "bounds": {"fonction": 60}}
    assert "12 ligne(s) jusqu'à 247 car." in out["warning"]


def test_set_schema_silent_without_bounds(store, monkeypatch):
    st, _ = store
    monkeypatch.setattr(dsm.db, "set_datastore_schema", lambda *a: None)
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups", lambda *a: [])
    # oto#82 : ce db stubbé ne porte aucun index — la pose reste donc faite.
    monkeypatch.setattr(dsm.db, "datastore_has_key_index", lambda ns_id: False)
    monkeypatch.setattr(dsm.db, "datastore_drop_key_index", lambda *a: None)
    monkeypatch.setattr(dsm.db, "datastore_overlong_fields",
                        lambda *a, **k: pytest.fail("aucune borne : pas de scan"))
    assert "warning" not in st.set_schema(
        "viviers", {"fields": [{"key": "notes", "type": "text"}]})
