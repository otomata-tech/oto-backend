"""La révision `20261119_2240_incidents_capacite` pose la table `org_incidents_capacite`.

Une table NEUVE que le démarrage pose aussi (même fragment) : la révision doit la poser
sur une base d'AVANT, avec son index unique partiel, ne rien faire sur une base que le
démarrage a déjà servie, et la retirer au retour arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261119_1300_app_use_role"
_REVISION = "20261119_2240_incidents_capacite"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _index(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return sorted(r[0] for r in c.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'org_incidents_capacite'"
        ).fetchall())


_POSEE = ["org_incidents_capacite_pkey", "uq_org_incidents_capacite_vivant"]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : table retirée, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE org_incidents_capacite")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_la_table_et_le_retour_arriere_la_retire(base_d_avant):
    from alembic import command

    assert _index(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _index(base_d_avant) == _POSEE
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _index(base_d_avant) == []
    command.upgrade(_alembic(), _REVISION)
    assert _index(base_d_avant) == _POSEE


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser la table avant."""
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    assert _index(base_d_avant), "la table est déjà là"
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # la table est là : IF NOT EXISTS
    assert _index(base_d_avant) == _POSEE


def test_un_seul_incident_vivant_par_org_et_famille(base_d_avant):
    """L'index partiel : deux incidents vivants de la même org et famille sont refusés,
    un vivant et un clos coexistent."""
    import psycopg
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    with psycopg.connect(base_d_avant, autocommit=True) as c:
        poser = ("INSERT INTO org_incidents_capacite (org_id, famille, cause, clos_at) "
                 "VALUES (1, 'claude_subscription', 'plafond', %s)")
        c.execute(poser, ("2026-11-19 10:00:00+00",))
        c.execute(poser, (None,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute(poser, (None,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO org_incidents_capacite (org_id, famille, cause) "
                      "VALUES (2, 'claude_subscription', 'inconnue')")
