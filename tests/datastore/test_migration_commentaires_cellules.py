"""La révision `20261120_1215_doc_cell_comments` pose les commentaires de page et la
cible élargie des commentaires de tableau (cellule, colonne).

Mêmes objets que le démarrage (même fragment, mêmes ordres) : la révision doit les poser
sur une base d'AVANT, ne rien faire sur une base que le démarrage a déjà servie, et les
retirer au retour arrière. Jouée sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[2]
_PRECEDENTE = "20261119_1600_repar_consentie"
_REVISION = "20261120_1215_doc_cell_comments"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _etat(dsn) -> dict:
    import psycopg
    with psycopg.connect(dsn) as c:
        un = lambda sql, *p: c.execute(sql, p).fetchone()  # noqa: E731
        return {
            "doc_comments": un("SELECT 1 FROM information_schema.tables "
                               "WHERE table_name = 'doc_comments'") is not None,
            "column_key": un("SELECT 1 FROM information_schema.columns WHERE table_name = "
                             "'datastore_row_comments' AND column_name = 'column_key'")
            is not None,
            "row_id_nullable": un("SELECT is_nullable FROM information_schema.columns WHERE "
                                  "table_name = 'datastore_row_comments' AND column_name = "
                                  "'row_id'")[0] == "YES",
            "contrainte": un("SELECT 1 FROM pg_constraint WHERE conname = "
                             "'datastore_row_comments_cible'") is not None,
            "index": un("SELECT 1 FROM pg_indexes WHERE indexname = "
                        "'idx_datastore_row_comments_colonne'") is not None,
        }


_AVANT = {"doc_comments": False, "column_key": False, "row_id_nullable": False,
          "contrainte": False, "index": False}
_APRES = {k: True for k in _AVANT}


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE doc_comments")
        c.execute("DROP INDEX idx_datastore_row_comments_colonne")
        c.execute("ALTER TABLE datastore_row_comments DROP CONSTRAINT "
                  "datastore_row_comments_cible")
        c.execute("DELETE FROM datastore_row_comments WHERE row_id IS NULL")
        c.execute("ALTER TABLE datastore_row_comments ALTER COLUMN row_id SET NOT NULL")
        c.execute("ALTER TABLE datastore_row_comments DROP COLUMN column_key")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_tout_et_le_retour_arriere_le_retire(base_d_avant):
    from alembic import command

    assert _etat(base_d_avant) == _AVANT
    command.upgrade(_alembic(), _REVISION)
    assert _etat(base_d_avant) == _APRES
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _etat(base_d_avant) == _AVANT
    command.upgrade(_alembic(), _REVISION)
    assert _etat(base_d_avant) == _APRES


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)
    assert _etat(base_d_avant) == _APRES


def test_le_demarrage_pose_la_meme_chose_sur_une_base_d_avant(base_d_avant):
    """Ordre indifférent : le démarrage seul amène une base d'avant au même état."""
    import psycopg
    from psycopg.rows import dict_row

    from oto_mcp.db._init import apply_boot_schema

    with psycopg.connect(base_d_avant, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS doc_comments")
        c.execute("DROP INDEX IF EXISTS idx_datastore_row_comments_colonne")
        c.execute("ALTER TABLE datastore_row_comments DROP CONSTRAINT IF EXISTS "
                  "datastore_row_comments_cible")
        c.execute("DELETE FROM datastore_row_comments WHERE row_id IS NULL")
        c.execute("ALTER TABLE datastore_row_comments ALTER COLUMN row_id SET NOT NULL")
        c.execute("ALTER TABLE datastore_row_comments DROP COLUMN IF EXISTS column_key")
    assert _etat(base_d_avant) == _AVANT
    with psycopg.connect(base_d_avant, row_factory=dict_row) as conn:
        with conn.transaction():
            apply_boot_schema(conn)
    assert _etat(base_d_avant) == _APRES
    # Rejoué sur une base à jour : rien ne change, rien ne lève.
    with psycopg.connect(base_d_avant, row_factory=dict_row) as conn:
        with conn.transaction():
            apply_boot_schema(conn)
    assert _etat(base_d_avant) == _APRES
