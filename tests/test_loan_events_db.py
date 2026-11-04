"""Le journal des prêts contre un VRAI PostgreSQL : la révision 0058 pose la table, que le
démarrage pose aussi. Plus rien n'y écrit depuis la bascule des comptes (#1499) : elle
garde l'historique des prêts d'avant, en lecture.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture()
def live(pg_dsn, monkeypatch):
    """Une base JETABLE par test, bootée par le vrai chemin."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_prets_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_pool = dbconn._pool
    monkeypatch.setenv("DATABASE_URL", dsn)
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _executer(dsn: str, *ordres: str):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        return [c.execute(o).fetchall() if o.lstrip().upper().startswith("SELECT")
                else c.execute(o) for o in ordres]


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


_COLONNES = ("SELECT column_name, data_type, is_nullable, column_default "
             "FROM information_schema.columns WHERE table_name = 'connector_loan_events' "
             "ORDER BY ordinal_position")


def test_la_revision_pose_la_table_du_demarrage_et_la_retire(live):
    from alembic import command
    (neuve,) = _executer(live, _COLONNES)
    assert neuve, "le démarrage pose la table sur une base neuve"
    _executer(live, "DROP TABLE connector_loan_events")
    command.stamp(_alembic(), "0057_contraintes_create_table")
    command.upgrade(_alembic(), "0058_journal_des_prets")
    assert _executer(live, _COLONNES)[0] == neuve
    command.downgrade(_alembic(), "0057_contraintes_create_table")
    assert _executer(live, _COLONNES)[0] == []
