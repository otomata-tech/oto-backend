"""La révision `20261104_1200_runner_jobs_finis` pose `idx_runner_jobs_conclus`.

Un index CONCURRENTLY que le démarrage pose aussi (même déclaration, `INDEX_CONCLUS`) :
la révision doit le poser VALIDE sur une base d'avant, ne rien faire sur une base que le
démarrage a déjà servie, et le retirer au retour arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261103_1400_guide_revisions"
_REVISION = "20261104_1200_runner_jobs_finis"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _validite(dsn):
    """`None` si l'index est absent, sinon sa validité."""
    import psycopg

    from oto_mcp.db.runner_jobs import INDEX_CONCLUS
    with psycopg.connect(dsn) as c:
        return c.execute(INDEX_CONCLUS.sql_validite).fetchone()


def test_la_revision_est_declaree_au_registre_des_index_concurrents():
    from oto_mcp.db import index_concurrent as ic
    from oto_mcp.db.runner_jobs import INDEX_CONCLUS, REVISION_CONCLUS

    assert REVISION_CONCLUS == _REVISION and len(_REVISION) <= 32
    assert ic.index_de_revision(_REVISION) == (INDEX_CONCLUS,)
    assert f"index-concurrents {_REVISION}" in INDEX_CONCLUS.procedure


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : index retiré, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP INDEX idx_runner_jobs_conclus")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_l_index_valide_et_le_retour_arriere_le_retire(base_d_avant):
    from alembic import command

    assert _validite(base_d_avant) is None
    command.upgrade(_alembic(), _REVISION)
    assert _validite(base_d_avant) == (True,)
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _validite(base_d_avant) is None
    command.upgrade(_alembic(), _REVISION)
    assert _validite(base_d_avant) == (True,)


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser l'index avant."""
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)    # l'index est là, valide : rien à faire
    assert _validite(base_d_avant) == (True,)
