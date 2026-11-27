"""La révision `20261126_1200_suggestions` pose les quatre colonnes des
suggestions sur `procedure_comments` et `doc_comments`.

Colonnes NEUVES, nullables sans défaut, que le démarrage pose aussi (même DDL) : la
révision doit les poser sur une base d'AVANT, ne rien faire sur une base que le
démarrage a déjà servie, et les retirer au retour arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261125_1710_doc_cell_comments"
_REVISION = "20261126_1200_suggestions"
_COLONNES = ("suggestion", "resolution", "resolved_by", "resolved_at")


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _colonnes(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return sorted(c.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name IN ('procedure_comments', 'doc_comments') "
            "AND column_name = ANY(%s)", (list(_COLONNES),)).fetchall())


_POSEES = sorted(
    (t, c, "timestamp with time zone" if c == "resolved_at" else "text", "YES", None)
    for t in ("procedure_comments", "doc_comments") for c in _COLONNES)


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : colonnes retirées, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        for t in ("procedure_comments", "doc_comments"):
            c.execute(f"ALTER TABLE {t} " + ", ".join(f"DROP COLUMN {x}" for x in _COLONNES))
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_les_colonnes_et_le_retour_arriere_les_retire(base_d_avant):
    from alembic import command

    assert _colonnes(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonnes(base_d_avant) == _POSEES
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _colonnes(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonnes(base_d_avant) == _POSEES


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser les colonnes avant."""
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    assert _colonnes(base_d_avant), "les colonnes sont déjà là"
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # les colonnes sont là : IF NOT EXISTS
    assert _colonnes(base_d_avant) == _POSEES
