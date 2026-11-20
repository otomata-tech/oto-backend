"""`scripts/figer_colonnes.py` — déclarer, une fois, les colonnes que les lignes portent
sans que le schéma les déclare (oto#124), avant que la garde ne les refuse.

Deux moitiés : le PLAN et la déduction du type, purs ; le PASSAGE sur une base — à
blanc, appliqué, idempotent, un schéma qui a bougé, un tableau sans schéma.
"""
from __future__ import annotations

import copy
import pathlib
import sys
import uuid

import pytest

from oto_mcp.datastore import types_inferes as ti

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from scripts import figer_colonnes as M  # noqa: E402


# ── la déduction et le plan, purs ────────────────────────────────────────────

@pytest.mark.parametrize("valeurs,attendu", [
    ([1, 2.5, None, ""], "number"),
    ([True, {"valeur": False, "comment": "vu"}], "bool"),
    (["2026-01-02", "2025-12-31"], "date"),
    (["2026-01-02", "bientôt"], "text"),
    (["a", "b"], "text"),
    ([["x"], []], "list"),
    ([{"a": 1}], "json"),
    (["12", 12], None),            # hétérogène : pas de type, rien de contraint
    (["75001", "06000"], "text"),  # des chiffres en CHAÎNE ne deviennent pas un nombre
    ([True, 1], None),
    ([None, "", [], {"comment": "seule couche"}], None),   # aucune valeur
])
def test_le_type_se_deduit_par_homogeneite_stricte(valeurs, attendu):
    assert ti.inferer(valeurs) == attendu


def test_le_plan_ajoute_les_colonnes_sans_toucher_au_reste():
    schema = {"key": "siren", "unknown_columns": "report",
              "fields": [{"key": "siren", "type": "text", "required": True}]}
    plan = M.figer(schema, {"rare": ["x"], "ca": [1, 2, 3], "siren.comment": ["c"],
                            "": ["vide"], "_x": [1]})
    assert plan.ajout == [{"key": "ca", "type": "number"}, {"key": "rare", "type": "text"}]
    assert plan.schema == {**schema, "fields": schema["fields"] + plan.ajout}
    assert schema["fields"] == [{"key": "siren", "type": "text", "required": True}]
    assert plan.non_colonnes == ["", "_x", "siren.comment"]
    assert M.figer(None, {"a": [1]}).schema == {"fields": [{"key": "a", "type": "number"}]}
    assert M.figer(schema, {}).vide


# ── le passage, sur une base ─────────────────────────────────────────────────

SUB = "u-figer"
SCHEMA = {"key": "siren", "unknown_columns": "report",
          "fields": [{"key": "siren", "type": "text"}]}


def _tableau(schema, lignes) -> int:
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "figer-" + uuid.uuid4().hex[:6])
    if schema is not None:
        db.set_datastore_schema(ns_id, copy.deepcopy(schema))
    for i, data in enumerate(lignes):
        db.datastore_insert_row(ns_id, f"r{i}-{uuid.uuid4().hex[:6]}", data)
    return ns_id


def _passage(appliquer, **filtres):
    lignes: list[str] = []
    bilan = M.executer(appliquer=appliquer, sortie=lignes.append, **filtres)
    return bilan, "\n".join(lignes)


LIGNES = [{"siren": "1", "ca": 10, "ville": "Lyon"},
          {"siren": "2", "ca": {"valeur": 20, "comment": "bilan"}, "note": "x"},
          {"siren": "3", "ca": 30, "ville": "Nice", "siren.comment": "relique"}]


def test_a_blanc_puis_applique_journalise_et_idempotent(live):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ns_id = _tableau(SCHEMA, LIGNES)
    bilan, texte = _passage(False, tableaux=[ns_id])
    assert db.get_datastore_by_id(ns_id)["schema"] == SCHEMA, "à blanc = rien d'écrit"
    assert bilan["a_figer"] == 1 and bilan["colonnes"] == 3, texte
    assert "ca (number, 3), ville (text, 2), note (text, 1)" in texte
    assert "`siren.comment`" in texte
    bilan, texte = _passage(True, tableaux=[ns_id])
    assert bilan["ecrits"] == 1, texte
    schema = db.get_datastore_by_id(ns_id)["schema"]
    assert schema["fields"] == SCHEMA["fields"] + [
        {"key": "ca", "type": "number"}, {"key": "ville", "type": "text"},
        {"key": "note", "type": "text"}]
    # Ni options, ni required, ni réglage de tête : seules les colonnes ont bougé.
    assert {k: v for k, v in schema.items() if k != "fields"} == {
        k: v for k, v in SCHEMA.items() if k != "fields"}
    with _connect() as conn:
        journal = conn.execute(
            "SELECT sub, args FROM tool_calls WHERE tool = 'data_set_schema' "
            "AND args->>'datastore' = %s", (str(ns_id),)).fetchall()
    assert len(journal) == 1 and journal[0]["sub"] is None
    assert journal[0]["args"]["migration_systeme"] == M.MIGRATION
    assert journal[0]["args"]["colonnes"] == "ca: number, ville: text, note: text"
    second, texte = _passage(False, tableaux=[ns_id])
    assert second["a_figer"] == 0, texte


def test_un_tableau_sans_schema_recoit_ses_colonnes(live):
    from oto_mcp import db
    ns_id = _tableau(None, [{"nom": "A", "vu": "2026-01-02"}, {"nom": "B", "n": [1]}])
    bilan, texte = _passage(True, tableaux=[ns_id])
    assert bilan["sans_schema_a_figer"] == 1 and "SANS schéma" in texte
    assert bilan["ecrits"] == 1, texte
    assert db.get_datastore_by_id(ns_id)["schema"] == {"fields": [
        {"key": "nom", "type": "text"}, {"key": "n", "type": "list", "of": {}},
        {"key": "vu", "type": "date"}]}
    assert _passage(False, tableaux=[ns_id])[0]["a_figer"] == 0


def test_apres_le_gel_les_colonnes_figees_s_ecrivent_et_les_neuves_non(live):
    """La raison d'être du gel : sous la garde, ce qui existait passe encore."""
    from oto_mcp.datastore import colonnes_non_declarees as cnd
    from oto_mcp.datastore.core import make_store
    ns_id = _tableau(None, [{"nom": "A", "age": 3}])
    _passage(True, tableaux=[ns_id])
    store = make_store(SUB)
    ns = str(ns_id)
    store.append_row(ns, {"nom": "B", "age": 4})
    with pytest.raises(cnd.ColonneNonDeclaree):
        make_store(SUB).append_row(ns, {"nom": "C", "taille": 180})


def test_un_schema_qui_a_bouge_est_saute(live, monkeypatch):
    from oto_mcp import db
    ns_id = _tableau(SCHEMA, LIGNES)
    vrai = M.inventaire

    def _perime(tableaux=None):
        lus = vrai(tableaux)
        db.set_datastore_schema(ns_id, {**SCHEMA, "description": "bougé"})
        return lus
    monkeypatch.setattr(M, "inventaire", _perime)
    bilan, texte = _passage(True, tableaux=[ns_id])
    assert bilan["bouges"] == [ns_id] and "SAUTÉ" in texte
    assert db.get_datastore_by_id(ns_id)["schema"] == {**SCHEMA, "description": "bougé"}


def test_un_tableau_illisible_est_liste_sans_arreter_le_parc(live, monkeypatch):
    ok = _tableau(None, [{"a": 1}])
    ko = _tableau(None, [{"b": 1}])
    vrai = M.valeurs_non_declarees

    def _lent(ns_id, declarees):
        if ns_id == ko:
            raise TimeoutError("statement_timeout")
        return vrai(ns_id, declarees)
    monkeypatch.setattr(M, "valeurs_non_declarees", _lent)
    bilan, texte = _passage(False, tableaux=[ok, ko])
    assert bilan["a_figer"] == 1 and [i for i, _ in bilan["non_lus"]] == [ko]
    assert f"--tableau {ko}" in texte
