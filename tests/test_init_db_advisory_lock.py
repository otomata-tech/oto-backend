"""Garde-fou : init_db prend un advisory lock de transaction EN PREMIER.

La DB est partagée canari/prod ; deux instances qui bootent en parallèle
exécutaient la même transaction de DDL et s'interbloquaient (DeadlockDetected
in init_db, Sentry). Le fix sérialise le boot par `pg_advisory_xact_lock`. Ce
lock ne protège que ce qui vient APRÈS lui → il doit être la toute première
instruction SQL de la transaction (avant même les _migrate_* legacy). Ce test
fige cet invariant sans base réelle (stub conn, arrêt après le 1er execute)."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

import oto_mcp.db._init as init_mod


class _Stop(Exception):
    """Interrompt init_db juste après avoir capté sa 1re instruction."""


def test_advisory_lock_is_first_statement(monkeypatch):
    seen: list[str] = []

    class _FakeConn:
        def execute(self, sql, params=None):
            seen.append(sql)
            raise _Stop  # on ne veut prouver QUE l'ordre du 1er statement

    @contextmanager
    def _fake_connect():
        yield _FakeConn()

    monkeypatch.setattr(init_mod, "_connect", _fake_connect)
    with pytest.raises(_Stop):
        init_mod.init_db()

    assert seen, "init_db n'a exécuté aucune instruction"
    assert "pg_advisory_xact_lock" in seen[0], (
        f"la 1re instruction de init_db doit être l'advisory lock, vu : {seen[0]!r}")
