"""La révision `20261103_1400_guide_revisions` pose la table, et l'écriture d'un guide y garde sa version.

Joué sur une vraie base : la révision pose la table sur une base d'AVANT, le retour
arrière la retire, et une fois posée, réécrire un guide y laisse l'état précédent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261103_1000_copilot_slack"
_REVISION = "20261103_1400_guide_revisions"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _existe(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT to_regclass('guide_revisions') AS t").fetchone()[0] is not None


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from alembic import command
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE guide_revisions")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_et_le_retour_arriere_retire(base_d_avant):
    from alembic import command
    assert not _existe(base_d_avant)
    command.upgrade(_alembic(), _REVISION)
    assert _existe(base_d_avant)
    command.downgrade(_alembic(), _PRECEDENTE)
    assert not _existe(base_d_avant)
    command.upgrade(_alembic(), _REVISION)


def test_une_reecriture_y_garde_l_etat_precedent(base_d_avant):
    from oto_mcp import db
    db.set_guide_db("user", "u-migr", "g", "v1", "T")
    db.set_guide_db("user", "u-migr", "g", "v2", "T", edited_by="u-migr")
    (v,) = db.guide_revisions.lister("user", "u-migr", "g", "on-demand")
    assert (v["rev"], v["reason"], v["edited_by"]) == (db.guide_rev("T", "", "v1"),
                                                       "write", "u-migr")
    assert db.guide_revisions.lire("user", "u-migr", "g", "on-demand",
                                   v["rev"])["body_md"] == "v1"


def test_la_raison_est_bornee(base_d_avant):
    import psycopg
    with psycopg.connect(base_d_avant) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO guide_revisions (owner_id, owner_type, slug, delivery, rev, "
                  "reason) VALUES ('u', 'user', 's', 'on-demand', 'r', 'autre')")
