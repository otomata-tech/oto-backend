"""La révision `20261103_1000_copilot_slack` pose `copilot_slack_threads` et
`copilot_slack_events`.

Deux tables NEUVES que le démarrage pose aussi (même fragment) : la révision doit les poser
sur une base d'AVANT, ne rien faire sur une base que le démarrage a déjà servie, et les
retirer au retour arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261102_1000_higgsfield_jobs"
_REVISION = "20261103_1000_copilot_slack"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _index(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return sorted(r[0] for r in c.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename IN "
            "('copilot_slack_threads', 'copilot_slack_events')").fetchall())


_POSEE = ["copilot_slack_events_pkey", "copilot_slack_threads_pkey",
          "idx_copilot_slack_events_created", "uq_copilot_slack_threads_cle"]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : tables retirées, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE copilot_slack_events")
        c.execute("DROP TABLE copilot_slack_threads")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_les_tables_et_le_retour_arriere_les_retire(base_d_avant):
    from alembic import command

    assert _index(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _index(base_d_avant) == _POSEE
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _index(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _index(base_d_avant) == _POSEE


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser les tables avant."""
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    assert _index(base_d_avant) == _POSEE
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)    # les tables sont là : IF NOT EXISTS
    assert _index(base_d_avant) == _POSEE
