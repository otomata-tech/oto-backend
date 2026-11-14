"""Les listes `calls` et `runs` de la lentille d'org honorent `limit`, leurs filtres et
un curseur (usage signal 1553).

Le constat d'un agent de supervision : `op=runs` rendait exactement 100 runs quelle
que soit la `limit` (100 ou 500), `sub` et `errors` n'y changeaient rien, et sans
curseur une fenêtre de 26 h restait hors d'atteinte — les runs en échec plus vieux
que la page étaient invisibles. `op=calls` avait le même mur (200, aucun curseur).

Contre un vrai PostgreSQL : un keyset, un LATERAL de clôture et deux lignes dans la
même seconde ne s'exercent pas contre un stub — un test qui n'inspecterait que la
chaîne SQL passerait au vert sur une requête qui saute des lignes. Tables créées avec
le DDL RÉEL extrait de `_schema.py`.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp.capabilities import org_monitoring as om
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.db import _schema, journal_calls, usage

ORG, AUTRE_ORG = 35, 36


def _real_ddl(table: str) -> str:
    m = re.search(rf"^CREATE TABLE IF NOT EXISTS {table} \(.*?^\);",
                  _schema._SCHEMA, re.S | re.M)
    assert m, f"DDL de `{table}` introuvable dans _schema.py"
    return m.group(0)


@pytest.fixture()
def conn(pg_module_dsn, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        for t in ("runs", "tool_calls", "users"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        for t in ("users", "tool_calls", "runs"):
            c.execute(_real_ddl(t))

        @contextmanager
        def _connect_test(*a, **kw):
            yield c

        monkeypatch.setattr(usage, "_connect", _connect_test)
        monkeypatch.setattr(usage, "_agregat", lambda objet, **kw: _connect_test())
        monkeypatch.setattr(journal_calls, "lecture_d_agregat",
                            lambda objet, **kw: _connect_test())
        # `sub` arrive en email OU sub : la résolution des membres n'est pas l'objet.
        monkeypatch.setattr(om.monitoring, "_resolve_sub", lambda t: t or None)
        yield c


_T0 = datetime.now(timezone.utc).replace(microsecond=0)


def _call(conn, *, at: datetime, tool="web_read", ok=True, sub="u1", org_id=ORG,
          run_id=None, args=None) -> None:
    conn.execute(
        "INSERT INTO tool_calls (created_at, kind, sub, tool, args, ok, run_id, org_id) "
        "VALUES (%s, 'mcp', %s, %s, %s::jsonb, %s, %s, %s)",
        (at, sub, tool, json.dumps(args or {}), ok, run_id, org_id))


def _run(conn, run_id, *, at: datetime, outcome=None, sub="u1", org_id=ORG) -> None:
    _call(conn, at=at, tool="run_start", sub=sub, org_id=org_id, run_id=run_id,
          args={"label": f"run {run_id}"})
    if outcome is not None:
        _call(conn, at=at + timedelta(seconds=30), tool="run_finish", sub=sub,
              org_id=org_id, run_id=run_id, args={"run_id": run_id, "outcome": outcome})


def _all_pages(op, **filtres) -> tuple[list[dict], int]:
    """Suit `next_cursor` jusqu'au bout. → (lignes concaténées, nombre de pages)."""
    lignes, cursor, pages = [], None, 0
    while True:
        out = om._console(None, om.OrgMonitoringInput(
            op=op, org_id=ORG, cursor=cursor, **filtres))
        pages += 1
        lignes += out[op]
        cursor = out["next_cursor"]
        if cursor is None:
            return lignes, pages
        assert pages < 100, "le curseur ne converge pas"


# ── runs ────────────────────────────────────────────────────────────────────

def test_runs_limit_is_honoured_beyond_100(conn):
    for i in range(130):
        _run(conn, f"r{i:03d}", at=_T0 - timedelta(minutes=i))
    out = om._console(None, om.OrgMonitoringInput(op="runs", org_id=ORG, limit=500))
    assert len(out["runs"]) == 130 and out["scanned"] == 130
    assert out["next_cursor"] is None
    # Écrêté au plafond servi, pas refusé : la page fait 500 au plus.
    assert om.OrgRunsInput(org_id=ORG, limit=5000).limit == usage.RUNS_LIMITE_MAX


def test_runs_cursor_walks_every_run_once_including_same_second(conn):
    # 23 runs, dont 5 ouverts dans la MÊME microseconde : le keyset départage par id.
    for i in range(18):
        _run(conn, f"r{i:02d}", at=_T0 - timedelta(minutes=i, microseconds=i * 7))
    meme = _T0 - timedelta(hours=1, microseconds=123)
    for i in range(5):
        _run(conn, f"same{i}", at=meme)
    lignes, pages = _all_pages("runs", limit=4)
    ids = [r["run_id"] for r in lignes]
    assert len(ids) == 23 and len(set(ids)) == 23          # ni saut, ni doublon
    assert pages == 6
    debuts = [r["started_at"] for r in lignes]
    assert debuts == sorted(debuts, reverse=True)           # plus récent d'abord


def test_runs_errors_reaches_a_failure_behind_a_full_page_of_successes(conn):
    """Le cas du signal : 500 runs verts récents, l'échec est derrière. Sans curseur
    il était invisible ; une page vide n'y vaut pas « aucun échec »."""
    for i in range(6):
        _run(conn, f"ok{i}", at=_T0 - timedelta(minutes=i), outcome="done")
    _run(conn, "rate", at=_T0 - timedelta(hours=20), outcome="failed")
    _run(conn, "moitie", at=_T0 - timedelta(hours=21), outcome="partial")
    _run(conn, "bloque", at=_T0 - timedelta(hours=22), outcome="blocked")
    _run(conn, "ouvert", at=_T0 - timedelta(hours=23))           # jamais clos

    first = om._console(None, om.OrgMonitoringInput(
        op="runs", org_id=ORG, limit=3, errors=True))
    assert first["runs"] == [] and first["scanned"] == 3
    assert first["next_cursor"] is not None                       # vide ≠ fini

    lignes, _ = _all_pages("runs", limit=3, errors=True)
    assert [r["run_id"] for r in lignes] == ["rate", "moitie", "bloque"]
    assert {r["outcome"] for r in lignes} == {"failed", "partial", "blocked"}


def test_runs_sub_and_days_filter_the_openings(conn):
    _run(conn, "a-recent", at=_T0 - timedelta(hours=1), sub="alice")
    _run(conn, "b-recent", at=_T0 - timedelta(hours=2), sub="bob")
    _run(conn, "a-vieux", at=_T0 - timedelta(days=3), sub="alice")
    _run(conn, "ailleurs", at=_T0 - timedelta(hours=1), sub="alice", org_id=AUTRE_ORG)

    def ids(**f):
        out = om._console(None, om.OrgMonitoringInput(op="runs", org_id=ORG, **f))
        return [r["run_id"] for r in out["runs"]]

    assert ids() == ["a-recent", "b-recent", "a-vieux"]
    assert ids(sub="alice") == ["a-recent", "a-vieux"]
    assert ids(days=2) == ["a-recent", "b-recent"]
    assert ids(sub="alice", days=2) == ["a-recent"]


def test_runs_rest_input_carries_the_same_filters(conn):
    """La route REST (`OrgRunsInput`) et l'op MCP servent la même page."""
    for i in range(5):
        _run(conn, f"r{i}", at=_T0 - timedelta(minutes=i),
             outcome="failed" if i % 2 else "done")
    rest = om._runs(None, om.OrgRunsInput(org_id=ORG, limit=10, errors=True))
    mcp = om._console(None, om.OrgMonitoringInput(op="runs", org_id=ORG, limit=10,
                                                  errors=True))
    assert rest == mcp
    assert [r["run_id"] for r in rest["runs"]] == ["r1", "r3"]


def test_list_runs_keeps_its_list_shape(conn):
    """Les autres lecteurs (`usage.runs` plateforme) reçoivent toujours une liste,
    sans les colonnes internes du keyset."""
    _run(conn, "r1", at=_T0)
    (run,) = usage.list_runs(100, org_id=ORG)
    assert "start_id" not in run and "_keyset_at" not in run


# ── calls ───────────────────────────────────────────────────────────────────

def test_calls_limit_is_honoured_beyond_200(conn):
    for i in range(260):
        _call(conn, at=_T0 - timedelta(seconds=i))
    out = om._console(None, om.OrgMonitoringInput(op="calls", org_id=ORG, limit=1000))
    assert len(out["calls"]) == 260 and out["next_cursor"] is None
    assert om.OrgCallsInput(org_id=ORG, limit=5000).limit == usage.CALLS_LIMITE_MAX


def test_calls_cursor_with_errors_covers_the_window_without_gap(conn):
    # Une erreur sur trois, dont plusieurs dans la MÊME seconde servie.
    attendu = []
    for i in range(40):
        at = _T0 - timedelta(minutes=i // 4, microseconds=(i % 4) * 10)
        _call(conn, at=at, ok=(i % 3 != 0), tool=f"t{i}")
        if i % 3 != 0:
            continue
        attendu.append(f"t{i}")
    _call(conn, at=_T0, ok=False, tool="autre-org", org_id=AUTRE_ORG)
    lignes, pages = _all_pages("calls", limit=5, errors=True)
    assert sorted(r["tool_name"] for r in lignes) == sorted(attendu)
    assert len({r["id"] for r in lignes}) == len(attendu)
    assert all(r["ok"] is False for r in lignes)
    assert pages == 3


def test_calls_sub_filter_is_threaded(conn):
    _call(conn, at=_T0, sub="alice", tool="a")
    _call(conn, at=_T0, sub="bob", tool="b")
    out = om._console(None, om.OrgMonitoringInput(op="calls", org_id=ORG, sub="alice"))
    assert [c["tool_name"] for c in out["calls"]] == ["a"]


# ── le curseur ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cursor", ["n'importe quoi", "eyJsIjoicnVucyJ9", "%%%"])
def test_unreadable_cursor_is_a_400(conn, cursor):
    for op in ("runs", "calls"):
        with pytest.raises(AuthzDenied) as e:
            om._console(None, om.OrgMonitoringInput(op=op, org_id=ORG, cursor=cursor))
        assert e.value.status == 400 and e.value.code == "invalid_cursor"


def test_cursor_from_another_org_or_lens_is_refused(conn):
    for i in range(3):
        _run(conn, f"r{i}", at=_T0 - timedelta(minutes=i))
    c = om._console(None, om.OrgMonitoringInput(op="runs", org_id=ORG, limit=1))
    cursor = c["next_cursor"]
    assert cursor
    for op, org in (("runs", AUTRE_ORG), ("calls", ORG)):
        with pytest.raises(AuthzDenied) as e:
            om._console(None, om.OrgMonitoringInput(op=op, org_id=org, cursor=cursor))
        assert e.value.code == "invalid_cursor"
