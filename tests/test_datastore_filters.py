"""Filtres par colonne du datastore (vue tableau dashboard, oto-dashboard#18).

Teste le constructeur de clauses `_ds_filter_clauses` / `_ds_where` (pur, sans DB) :
whitelist d'ops, paramétrage du champ (anti-injection), numérique vs texte pour les
comparaisons ordonnées, AND combiné, et le partage list/count (total cohérent).
"""
from __future__ import annotations

import pytest

from oto_mcp import db


def test_no_filters_is_noop():
    clauses, params = db._ds_filter_clauses(None)
    assert clauses == [] and params == []
    clauses, params = db._ds_filter_clauses([])
    assert clauses == [] and params == []


def test_contains_eq_in():
    clauses, params = db._ds_filter_clauses([
        {"field": "secteur", "op": "contains", "value": "santé"},
        {"field": "statut", "op": "eq", "value": "retenu"},
        {"field": "offre", "op": "in", "value": ["sante_prevoyance", "titres_restaurant"]},
    ])
    assert clauses[0] == "data ->> %s ILIKE %s"
    assert params[0:2] == ["secteur", "%santé%"]
    assert clauses[1] == "data ->> %s = %s"
    assert params[2:4] == ["statut", "retenu"]
    assert clauses[2] == "data ->> %s = ANY(%s)"
    assert params[4] == "offre" and params[5] == ["sante_prevoyance", "titres_restaurant"]


def test_numeric_comparison_casts_and_guards():
    # Valeur numérique → cast ::numeric gardé (champ apparaît 2× : regex + cast).
    clauses, params = db._ds_filter_clauses([{"field": "effectif", "op": "gte", "value": "50"}])
    assert "::numeric >= %s::numeric" in clauses[0]
    assert "~ '^-?[0-9]+" in clauses[0]
    assert params == ["effectif", "effectif", "50"]


def test_date_comparison_stays_textual():
    # Valeur non numérique (ISO date) → comparaison texte (lexicographique = chrono).
    clauses, params = db._ds_filter_clauses([{"field": "date_depot", "op": "lt", "value": "2024-01-01"}])
    assert clauses[0] == "data ->> %s < %s"
    assert params == ["date_depot", "2024-01-01"]


def test_empty_not_empty():
    clauses, params = db._ds_filter_clauses([
        {"field": "email", "op": "empty", "value": None},
        {"field": "phone", "op": "not_empty", "value": None},
    ])
    assert clauses[0] == "(data ->> %s IS NULL OR data ->> %s = '')"
    assert params[0:2] == ["email", "email"]
    assert clauses[1] == "(data ->> %s IS NOT NULL AND data ->> %s <> '')"
    assert params[2:4] == ["phone", "phone"]


def test_field_is_always_parameterized_no_injection():
    # Un nom de champ hostile ne doit JAMAIS apparaître dans le SQL — il part en param.
    evil = "x'); DROP TABLE datastore_rows; --"
    clauses, params = db._ds_filter_clauses([{"field": evil, "op": "eq", "value": "1"}])
    assert evil not in clauses[0]
    assert clauses[0] == "data ->> %s = %s"
    assert params[0] == evil


def test_bad_op_and_shape_raise():
    with pytest.raises(ValueError):
        db._ds_filter_clauses([{"field": "a", "op": "nope", "value": "1"}])
    with pytest.raises(ValueError):
        db._ds_filter_clauses([{"field": "", "op": "eq", "value": "1"}])
    with pytest.raises(ValueError):
        db._ds_filter_clauses(["not-a-dict"])
    with pytest.raises(ValueError):
        db._ds_filter_clauses([{"field": "a", "op": "eq", "value": "1"}] * (db._DS_MAX_FILTERS + 1))


def test_where_merges_q_and_filters_in_order():
    where, params = db._ds_where(7, "marseille", [{"field": "statut", "op": "eq", "value": "retenu"}])
    assert where == ("WHERE ns_id = %s AND data::text ILIKE %s "
                     "AND data ->> %s = %s")
    assert params == [7, "%marseille%", "statut", "retenu"]
