"""Datastore — relevé des champs écrits HORS SCHÉMA (oto-backend#294).

Le cas fondateur : un schéma renommé (`actualite_sociale` → `analyse1`), un agent
qui continue d'écrire sous les anciens noms. Rien ne le refuse (un champ libre est
un droit du contrat 0016) et rien n'est perdu — mais la valeur atterrit dans une
colonne hors format, que l'interface et tout ce qui s'appuie sur le schéma
ignorent, et l'agent reçoit un accusé de réception. D'où ce relevé, rendu dans la
réponse d'écriture : ce que l'appelant peut VÉRIFIER.

Deux étages : la fonction pure (`datastore_schema`) et le relevé du store (le seam
`_check_row`, par lequel passent append / batch / merge / upsert / patch).
"""
import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.core import DatastorePg, RowValidationError


STRICT = {
    "strict": True,
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "analyse1", "type": "text"},
        {"key": "analyse2", "type": "text"},
        {"key": "occupant", "type": "object",
         "fields": [{"key": "nom", "type": "text"}]},
        {"key": "contacts", "type": "list",
         "of": {"fields": [{"key": "nom", "type": "text"}]}},
    ],
}
SOFT = {"fields": [{"key": "siren", "type": "text"}]}


# ── fonction pure ────────────────────────────────────────────────────────────

def test_unknown_key_is_reported_on_strict():
    assert dsv2.off_schema_keys(
        STRICT, {"siren": "1", "champ_qui_nexiste_pas": "test"}
    ) == ["champ_qui_nexiste_pas"]


def test_the_founding_case_renamed_fields():
    """Les 4 anciens noms du renommage, tous nommés d'un coup."""
    assert dsv2.off_schema_keys(STRICT, {
        "siren": "1", "actualite_sociale": "…", "actualite_business": "…",
        "priorites_rh": "…", "prix_faits_marquants": "…",
    }) == ["actualite_business", "actualite_sociale", "priorites_rh",
           "prix_faits_marquants"]


def test_soft_schema_reports_nothing():
    """Hors strict, un champ libre est un droit explicite du contrat — pas une
    anomalie : c'est ce qui permet d'explorer un tableau avant de le typer."""
    assert dsv2.off_schema_keys(SOFT, {"siren": "1", "libre": "x"}) == []
    assert dsv2.off_schema_keys(None, {"libre": "x"}) == []


def test_strict_without_fields_reports_nothing():
    """Sans référentiel, TOUT serait hors schéma — ça n'informe personne."""
    assert dsv2.off_schema_keys({"strict": True}, {"a": 1, "b": 2}) == []


def test_declared_keys_are_never_reported():
    assert dsv2.off_schema_keys(STRICT, {"siren": "1", "analyse1": "a",
                                         "analyse2": "b"}) == []


def test_sub_records_use_dotted_paths_and_aggregate_lists():
    keys = dsv2.off_schema_keys(STRICT, {
        "occupant": {"nom": "ACME", "naf": "62.01Z"},
        "contacts": [{"nom": "A", "email": "a@x.fr"},
                     {"nom": "B", "email": "b@x.fr", "tel": "06"}],
    })
    # une entrée par CHEMIN, pas par item de liste
    assert keys == ["contacts[].email", "contacts[].tel", "occupant.naf"]


def test_an_off_schema_field_is_not_explored():
    """On ne descend pas dans un champ déjà hors schéma : on ne sait pas ce qu'il
    devrait contenir, et le nommer une fois suffit."""
    assert dsv2.off_schema_keys(
        STRICT, {"bloc_inconnu": {"a": 1, "b": {"c": 2}}}) == ["bloc_inconnu"]


def test_warning_names_the_fields_and_says_what_to_do():
    assert dsv2.off_schema_warning([]) is None
    msg = dsv2.off_schema_warning(["actualite_sociale"])
    assert "`actualite_sociale`" in msg and "data_get_schema" in msg


# ── relevé du store ──────────────────────────────────────────────────────────

@pytest.fixture()
def store(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    calls = {"insert": [], "update": []}
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": STRICT})
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data, *a, **k: (
                            calls["insert"].append(data) or
                            {"row_id": rid, "created_at": "t", "updated_at": "t",
                             "data": data}))
    # #317 : l'écriture ciblée contrôle le bail avant d'écrire. Ces tests portent
    # sur l'identité de la ligne visée, pas sur le verrou — elle y est libre.
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
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
    return st, calls


def test_append_accepts_the_value_and_reports_the_field(store):
    """Le comportement ne change pas — la valeur EST écrite ; ce qui change, c'est
    qu'on le dit."""
    st, calls = store
    out = st.append_row("v", {"siren": "1", "actualite_sociale": "…"})
    assert out["actualite_sociale"] == "…"          # accepté, persisté
    assert calls["insert"][0]["actualite_sociale"] == "…"
    assert st.off_schema_report() == {
        "hors_schema": ["actualite_sociale"],
        "hors_schema_hint": dsv2.off_schema_warning(["actualite_sociale"]),
    }


def test_conforming_write_reports_nothing(store):
    """Pas de clé parasite dans la réponse quand tout est dans le format."""
    st, _ = store
    st.append_row("v", {"siren": "1", "analyse1": "a"})
    assert st.off_schema_report() == {}


def test_batch_aggregates_the_lot_once(store):
    st, calls = store
    st.write_rows("v", [{"siren": "1", "actualite_sociale": "a"},
                        {"siren": "2", "actualite_sociale": "b"},
                        {"siren": "3", "priorites_rh": "c"}])
    assert len(calls["insert"]) == 3
    assert st.off_schema_report()["hors_schema"] == ["actualite_sociale",
                                                    "priorites_rh"]


def test_patch_judges_only_the_keys_written(store, monkeypatch):
    """Une colonne hors schéma DÉJÀ en base ne doit pas ré-alerter à chaque patch
    d'un autre champ (même raison que la borne max_length, #383) : le relevé porte
    sur ce que le geste POSE."""
    st, _ = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"siren": "1",
                                                     "actualite_sociale": "vieux"}})
    st.update_row("v", "r1", {"analyse1": "propre"})
    assert st.off_schema_report() == {}
    st.update_row("v", "r1", {"priorites_rh": "encore hors format"})
    assert st.off_schema_report()["hors_schema"] == ["priorites_rh"]


def test_refused_row_reports_nothing(store, monkeypatch):
    """Une écriture refusée n'a rien posé — le relevé ne parle que d'écritures
    acceptées."""
    st, calls = store
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": {
                            "strict": True,
                            "fields": [{"key": "siren", "type": "text",
                                        "required": True}]}})
    with pytest.raises(RowValidationError):
        st.append_row("v", {"champ_inconnu": "x"})
    assert calls["insert"] == []
    assert st.off_schema_report() == {}
