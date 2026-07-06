"""Projection de colonnes de `data_rows` (feedback #191). `_project_row` trime une
row sur un sous-ensemble de colonnes, en gardant TOUJOURS `_id` (adressabilité)."""
from __future__ import annotations

from oto_mcp.tools import datastore as D

_ROW = {"_id": "abc", "_created_at": "t", "_updated_at": "t",
        "denomination": "ACME", "commune": "Lyon", "kwc": 42, "email": "a@b.c"}


def test_projects_subset():
    out = D._project_row(_ROW, ["denomination", "kwc"])
    assert out == {"_id": "abc", "denomination": "ACME", "kwc": 42}


def test_always_keeps_id_even_if_not_requested():
    out = D._project_row(_ROW, ["commune"])
    assert out["_id"] == "abc"
    assert set(out) == {"_id", "commune"}


def test_unknown_field_is_omitted_not_error():
    out = D._project_row(_ROW, ["denomination", "inexistant"])
    assert out == {"_id": "abc", "denomination": "ACME"}


def test_empty_fields_keeps_only_id():
    out = D._project_row(_ROW, [])
    assert out == {"_id": "abc"}
