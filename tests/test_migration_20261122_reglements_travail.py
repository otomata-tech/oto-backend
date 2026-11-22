"""La révision `20261122_1200_reglements_travail` pose `idx_credit_movements_travail`.

Un index CONCURRENTLY que le démarrage pose aussi (même déclaration,
`INDEX_REGLEMENTS_PAR_TRAVAIL`) : la révision doit le poser VALIDE sur une base d'avant,
ne rien faire sur une base que le démarrage a déjà servie, et le retirer au retour
arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261119_1600_repar_consentie"
_REVISION = "20261122_1200_reglements_travail"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _validite(dsn):
    """`None` si l'index est absent, sinon sa validité."""
    import psycopg

    from oto_mcp.db.credits import INDEX_REGLEMENTS_PAR_TRAVAIL
    with psycopg.connect(dsn) as c:
        return c.execute(INDEX_REGLEMENTS_PAR_TRAVAIL.sql_validite).fetchone()


def test_la_revision_est_declaree_au_registre_des_index_concurrents():
    from oto_mcp.db import index_concurrent as ic
    from oto_mcp.db.credits import (INDEX_REGLEMENTS_PAR_TRAVAIL,
                                    REVISION_REGLEMENTS_PAR_TRAVAIL)

    assert REVISION_REGLEMENTS_PAR_TRAVAIL == _REVISION and len(_REVISION) <= 32
    assert ic.index_de_revision(_REVISION) == (INDEX_REGLEMENTS_PAR_TRAVAIL,)
    assert f"index-concurrents {_REVISION}" in INDEX_REGLEMENTS_PAR_TRAVAIL.procedure


def test_le_predicat_de_l_index_est_celui_de_la_lecture():
    """Un index partiel ne sert que la requête dont le WHERE implique son prédicat : la
    lecture des jetons des travaux le porte mot pour mot."""
    import inspect

    from oto_mcp.db import credits, runner_jobs
    assert credits.PREDICAT_REGLEMENT_DE_TRAVAIL in credits.INDEX_REGLEMENTS_PAR_TRAVAIL.forme
    assert "PREDICAT_REGLEMENT_DE_TRAVAIL" in inspect.getsource(runner_jobs.jetons_des_travaux)


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : index retiré, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP INDEX idx_credit_movements_travail")
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
