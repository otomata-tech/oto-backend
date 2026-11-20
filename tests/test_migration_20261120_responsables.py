"""La révision `20261120_1108_responsables` pose `docs.owner_sub` et
`org_instructions.owner_sub`.

Le responsable d'une page et d'une procédure. Deux colonnes NEUVES, nullables sans
défaut, que le démarrage pose aussi (même DDL) : la révision doit les poser sur une base
d'AVANT (qui ne les a pas), ne rien faire sur une base que le démarrage a déjà servie,
et les retirer au retour arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261119_1600_repar_consentie"
_REVISION = "20261120_1108_responsables"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _colonnes(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return sorted(c.execute(
            "SELECT table_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name IN ('docs', 'org_instructions') AND column_name = 'owner_sub'"
        ).fetchall())


_POSEES = [("docs", "text", "YES", None), ("org_instructions", "text", "YES", None)]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : colonnes retirées, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE docs DROP COLUMN owner_sub")
        c.execute("ALTER TABLE org_instructions DROP COLUMN owner_sub")
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
