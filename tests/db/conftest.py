"""Une base PostgreSQL neuve par test ; aucune configuration de base servie utilisée."""
import uuid

import pytest


@pytest.fixture
def base_fraiche(pg_dsn, monkeypatch):
    """Une base vide, bootée par le VRAI `init_db`, détruite à la sortie.

    Elle ne migre rien — elle isole. Un test qui vérifie ce qu'une écriture laisse
    en base ne peut pas le faire sur la base partagée du reste de la suite : il y
    lirait le résidu des voisins, et son vert ne prouverait rien.
    """
    import psycopg
    from psycopg.rows import dict_row
    from oto_mcp.db import _conn, _init

    name = "oto_projections_" + uuid.uuid4().hex[:12]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setattr(_conn, "_pool", None)
    try:
        _init.init_db()
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            yield conn
    finally:
        if _conn._pool is not None:
            _conn._pool.close()
        root.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        root.close()
