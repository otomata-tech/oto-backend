"""File de travail v2 (ADR 0046 D) — surface de SUPERVISION (dashboard) :
`queue` (rows sous bail, lecture) + `force_release` (libération sans garde de
worker, écriture). Seams PG monkeypatchés — le chemin SQL est vérifié au deploy."""
from __future__ import annotations

from oto_mcp import datastore as D


def test_queue_lists_claimed_rows_read_only(monkeypatch):
    seen = {}
    rows = [{"row_id": "r1", "created_at": "c", "updated_at": "u",
             "data": {"nom": "ACME"}, "claimed_by": "w-1", "claimed_until": "t"}]

    def _resolve(ns, write=False):
        seen["write"] = write
        return 7

    monkeypatch.setattr(D.db, "datastore_claimed_rows", lambda ns_id: rows)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", _resolve)

    out = s.queue("vivier")
    assert out == [{"_id": "r1", "_created_at": "c", "_updated_at": "u",
                    "nom": "ACME", "_claimed_by": "w-1", "_claimed_until": "t"}]
    assert seen["write"] is False  # supervision = lecture, pas d'écriture exigée


def test_force_release_requires_write_and_skips_worker_guard(monkeypatch):
    seen = {}

    def _resolve(ns, write=False):
        seen["write"] = write
        return 7

    def _release(ns_id, row_id, worker):
        seen.update(ns_id=ns_id, row_id=row_id, worker=worker)
        return True

    monkeypatch.setattr(D.db, "datastore_release_claim", _release)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", _resolve)

    assert s.force_release("vivier", "r1") is True
    assert seen["write"] is True   # écriture exigée (on touche le bail)
    assert seen["worker"] is None  # libération inconditionnelle (supervision humaine)
    assert (seen["ns_id"], seen["row_id"]) == (7, "r1")
