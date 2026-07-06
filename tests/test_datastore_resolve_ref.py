"""resolve_datastore_ns accepte id OU nom (fix « Aperçu indisponible » : le picker projet
stocke le target_ref = id numérique, l'endpoint résolvait par nom → 404). On capture les
params passés à SQL — un ref tout-chiffres pose `nsid` (int), un nom laisse `nsid=None`.
La sémantique SQL réelle (anti-IDOR, préférence nom) est validée contre un vrai Postgres."""
from __future__ import annotations

import contextlib

from oto_mcp.db import datastore as DB


class _Cur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, cap, row):
        self.cap, self.row = cap, row

    def execute(self, sql, params=None):
        self.cap["sql"] = sql
        self.cap["params"] = params
        return _Cur(self.row)


def _patch(monkeypatch, cap, row=None):
    @contextlib.contextmanager
    def _fake():
        yield _Conn(cap, row)
    monkeypatch.setattr(DB, "_connect", _fake)


def test_digit_ref_sets_nsid_int(monkeypatch):
    cap = {}
    _patch(monkeypatch, cap, row={"id": 109})
    DB.resolve_datastore_ns("109", sub="u1", org_ids=[42], group_ids=[])
    assert cap["params"]["nsid"] == 109          # id numérique posé
    assert cap["params"]["ns"] == "109"          # nom conservé aussi (OR)
    assert "d.id = %(nsid)s" in cap["sql"]


def test_name_ref_leaves_nsid_none(monkeypatch):
    cap = {}
    _patch(monkeypatch, cap, row={"id": 5})
    DB.resolve_datastore_ns("vivier-pmi", sub="u1", org_ids=[42], group_ids=[])
    assert cap["params"]["nsid"] is None         # pas un id → NULL → jamais de match id
    assert cap["params"]["ns"] == "vivier-pmi"


def test_visibility_predicate_still_present(monkeypatch):
    # anti-IDOR : le prédicat de visibilité (owner/org/grant) reste dans le WHERE.
    cap = {}
    _patch(monkeypatch, cap, row=None)
    DB.resolve_datastore_ns("109", sub="u1", org_ids=[42], group_ids=[])
    sql = cap["sql"]
    assert "resource_grants" in sql and "d.owner_id = %(sub)s" in sql
