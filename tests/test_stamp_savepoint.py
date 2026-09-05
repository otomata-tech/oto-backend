"""#333 — l'échec du stamp ne doit jamais emporter l'écriture métier.

Le stamp du vecteur de rang (#318) s'exécute DANS la transaction de l'écriture
qui vient de modifier la ligne, et son erreur est avalée — best-effort assumé.
Mais avaler l'exception ne répare pas la transaction : PostgreSQL l'a AVORTÉE,
et le COMMIT de sortie devient un ROLLBACK silencieux. L'écriture métier
s'évapore pendant que l'appelant reçoit l'écho du RETURNING, rendu avant
l'avortement : la fonction répond « écrit » pour une ligne qui n'existera
jamais.

Banc : une erreur SQL pendant le RECALCUL, sur un schéma complet. L'invalidation
préalable doit laisser NULL ; sans savepoint, tout tombe. Depuis le maintien des
projections, une colonne absente est une erreur de schéma qui refuse l'écriture,
pas une optimisation qu'on peut sauter. La vérité se lit par une
connexion FRAÎCHE (`datastore_get_row`) — jamais par l'écho, c'est lui qui
ment.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture(scope="module")
def base_vecteur(pg_dsn):
    """Une base bootée par le VRAI `init_db`, avec toutes les colonnes requises."""
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_stamp_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp.db._conn import _connect
        with _connect() as conn:
            ns = conn.execute(
                "INSERT INTO user_datastores (owner_type, owner_id, namespace) "
                "VALUES ('user', 'banc', 'banc_333') RETURNING id").fetchone()
        yield int(ns["id"])
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture
def recalcul_vecteur_en_echec(base_vecteur, monkeypatch):
    from oto_mcp.db import search
    monkeypatch.setattr(search, "_vec", lambda expr: "missing_rank_function()")
    return base_vecteur


def test_linsert_persiste_malgre_lechec_du_stamp(recalcul_vecteur_en_echec):
    from oto_mcp.db.datastore import datastore_get_row, datastore_insert_row

    ns_id = recalcul_vecteur_en_echec
    echo = datastore_insert_row(ns_id, "r1", {"nom": "durand"})
    assert echo["data"] == {"nom": "durand"}, "l'écho, lui, a toujours dit vrai"

    relu = datastore_get_row(ns_id, "r1")
    assert relu is not None, \
        "la ligne annoncée écrite n'existe pas : le COMMIT était un ROLLBACK (#333)"
    assert relu["data"] == {"nom": "durand"}


def test_lupdate_persiste_et_lecho_dit_vrai(recalcul_vecteur_en_echec):
    from oto_mcp.db.datastore import (datastore_get_row, datastore_insert_row,
                                      datastore_update_row)

    ns_id = recalcul_vecteur_en_echec
    datastore_insert_row(ns_id, "r2", {"etat": "avant"})
    echo = datastore_update_row(ns_id, "r2", {"etat": "apres"},
                                "2026-08-14T00:00:00Z")
    assert echo is not None and echo["data"] == {"etat": "apres"}

    relu = datastore_get_row(ns_id, "r2")
    assert relu["data"] == {"etat": "apres"}, \
        "l'update annoncé a été roulé en arrière par l'échec du stamp (#333)"


def test_lupsert_persiste_malgre_lechec_du_stamp(recalcul_vecteur_en_echec):
    from oto_mcp.db.datastore import datastore_get_row, datastore_upsert_row

    ns_id = recalcul_vecteur_en_echec
    _, inserted = datastore_upsert_row(ns_id, "r3", {"v": 1})
    assert inserted is True
    relu = datastore_get_row(ns_id, "r3")
    assert relu is not None and relu["data"] == {"v": 1}


def test_invalidation_impossible_refuse_lecriture(base_vecteur):
    import psycopg
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.datastore import datastore_get_row, datastore_insert_row
    with _connect() as conn:
        conn.execute("ALTER TABLE datastore_rows DROP COLUMN search_vec")
    try:
        with pytest.raises(psycopg.errors.UndefinedColumn):
            datastore_insert_row(base_vecteur, "invalid-schema", {"nom": "refusé"})
        assert datastore_get_row(base_vecteur, "invalid-schema") is None
    finally:
        with _connect() as conn:
            conn.execute("ALTER TABLE datastore_rows ADD COLUMN search_vec tsvector")
