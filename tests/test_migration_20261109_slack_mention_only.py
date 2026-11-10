"""La révision `20261109_2100_slack_mention_only` pose `orgs.slack_mention_only`.

Le réglage Slack « mention seule » d'une org. Une colonne NEUVE, `BOOLEAN NOT NULL
DEFAULT FALSE`, que le démarrage pose aussi (même DDL) : la révision doit la poser sur
une base d'AVANT (chaque org existante à FALSE — le comportement d'avant), ne rien faire
sur une base que le démarrage a déjà servie, et la retirer au retour arrière. Joué sur
une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261108_1200_procedure_comments"
_REVISION = "20261109_2100_slack_mention_only"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def test_la_revision_est_au_registre_et_suit_la_precedente():
    from alembic.script import ScriptDirectory
    rev = ScriptDirectory.from_config(_alembic()).get_revision(_REVISION)
    assert rev is not None and rev.down_revision == _PRECEDENTE
    assert len(_REVISION) <= 32 and Path(rev.path).stem == _REVISION


def _colonne(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'orgs' AND column_name = 'slack_mention_only'"
        ).fetchall()


_POSEE = [("boolean", "NO", "false")]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : une org, colonne retirée, estampillée
    à la révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        oid = c.execute("INSERT INTO orgs (name) VALUES ('org d''avant') RETURNING id"
                        ).fetchone()[0]
        c.execute("ALTER TABLE orgs DROP COLUMN slack_mention_only")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn, oid


def test_la_revision_pose_la_colonne_a_FALSE_et_le_retour_arriere_la_retire(base_d_avant):
    import psycopg
    from alembic import command

    dsn, oid = base_d_avant
    assert _colonne(dsn) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonne(dsn) == _POSEE
    with psycopg.connect(dsn) as c:
        v = c.execute("SELECT slack_mention_only FROM orgs WHERE id = %s", (oid,)).fetchone()
    assert v == (False,), "une org d'avant garde le comportement d'avant"
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _colonne(dsn) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonne(dsn) == _POSEE


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser la colonne avant."""
    from alembic import command

    dsn, _ = base_d_avant
    command.upgrade(_alembic(), _REVISION)
    assert _colonne(dsn), "la colonne est déjà là"
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # la colonne est là : IF NOT EXISTS
    assert _colonne(dsn) == _POSEE
