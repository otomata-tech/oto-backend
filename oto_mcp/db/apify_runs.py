"""Apify runs launched on the platform key — table `apify_runs`
(DDL: `schema/usage.py::APIFY_RUNS`).

A run outlives the call that started it, and only a read of the FINISHED run traces its
cost. This table records the run at launch, so a run nobody reads back is still found:
`apify_releve.py` settles it under the org that launched it.

NOT flattened into `db.__init__` (`noter`, `lire` and `marquer` are too common for that
surface) — callers write `db.apify_runs.<fn>`.

⚠️ Synchronous (psycopg): an async caller goes through `run_in_threadpool`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

#: How long a settled run stays in the table: it serves the monthly comparison, the
#: journal keeps the billed line.
RETENTION_JOURS = 120


def noter(apify_run_id: str, *, org_id: Optional[int], sub: Optional[str], outil: str,
          actor_id: str, run_id: Optional[str], plafond_usd: Optional[float]) -> None:
    """The run was started. A second write of the same run keeps the first."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO apify_runs (apify_run_id, org_id, sub, outil, actor_id, run_id, "
            "plafond_usd) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (apify_run_id) DO NOTHING",
            (apify_run_id, org_id, sub, outil, actor_id, run_id, plafond_usd))


def lire(apify_run_id: str) -> Optional[dict]:
    """The recorded run, or None for a run launched outside this table."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM apify_runs WHERE apify_run_id = %s",
                           (apify_run_id,)).fetchone()
    return dict(row) if row else None


def marquer(apify_run_id: str, *, statut: str, cout_usd: Optional[float]) -> None:
    """The run's final state. The first figure stays: the biller keeps it too."""
    with _connect() as conn:
        conn.execute(
            "UPDATE apify_runs SET releve_le = NOW(), statut = %s, cout_usd = %s "
            "WHERE apify_run_id = %s AND releve_le IS NULL",
            (statut, cout_usd, apify_run_id))


def a_relever(limite: int = 500) -> list[dict]:
    """The runs without a final cost, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM apify_runs WHERE releve_le IS NULL "
            "ORDER BY lance_le LIMIT %s", (limite,)).fetchall()
    return [dict(r) for r in rows]


def bilan_depuis(depuis: str) -> dict:
    """What the table knows since `depuis`: `releve_usd`, the settled total; `en_attente`,
    the runs without a cost yet; `premier`, the first run the table holds at all (None
    while empty) — a comparison that starts before it would count what it never saw."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cout_usd), 0) AS releve_usd, "
            "       COUNT(*) FILTER (WHERE releve_le IS NULL) AS en_attente "
            "FROM apify_runs WHERE lance_le >= %s::timestamptz", (depuis,)).fetchone()
        premier = conn.execute("SELECT MIN(lance_le) AS premier FROM apify_runs").fetchone()
    return {"releve_usd": float(row["releve_usd"]), "en_attente": int(row["en_attente"]),
            "premier": premier["premier"]}


def purger(jours: int = RETENTION_JOURS) -> int:
    """Drop the SETTLED runs older than `jours`. A run without a cost is never dropped."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM apify_runs WHERE releve_le IS NOT NULL "
            "AND lance_le < NOW() - make_interval(days => %s)", (jours,))
    return cur.rowcount
