"""La révision `20261026_1337_copilot` pose les trois tables de l'agent intégré.

Trois tables NEUVES que le démarrage pose aussi (même fragment) : la révision doit les
poser sur une base d'AVANT, ne rien faire sur une base que le démarrage a déjà servie,
et les retirer au retour arrière. Jouée sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261026_1256_apps"
_REVISION = "20261026_1337_copilot"
_TABLES = ["copilot_spend", "copilot_threads", "copilot_undos"]


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _tables(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return sorted(r[0] for r in c.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = ANY(%s)", (_TABLES,)).fetchall())


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : tables retirées, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        # The Slack filing (`20261103_1000_copilot_slack`) rests on the conversations.
        c.execute("DROP TABLE IF EXISTS copilot_slack_threads")
        c.execute("DROP TABLE copilot_spend")
        c.execute("DROP TABLE copilot_undos")
        c.execute("DROP TABLE copilot_threads")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_les_tables_et_le_retour_arriere_les_retire(base_d_avant):
    from alembic import command

    assert _tables(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _tables(base_d_avant) == _TABLES
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _tables(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _tables(base_d_avant) == _TABLES


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)     # IF NOT EXISTS
    assert _tables(base_d_avant) == _TABLES
