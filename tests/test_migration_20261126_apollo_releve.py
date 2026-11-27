"""La révision `20261126_1200_apollo_releve` pose `apollo_phone_reveals.releve_le`.

Elle suit la tête de main (`20261125_1103_revocations`) : une seule tête. Sur une base
d'AVANT (colonne absente) : la colonne naît nullable, sans défaut ; rejouée, elle ne
fait rien ; le retour arrière la retire. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261125_1103_revocations"
_REVISION = "20261126_1200_apollo_releve"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def test_la_revision_est_au_registre_et_suit_la_tete_de_main():
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(_alembic())
    rev = script.get_revision(_REVISION)
    assert rev is not None and rev.down_revision == _PRECEDENTE
    assert len(_REVISION) <= 32 and Path(rev.path).stem == _REVISION
    assert script.get_heads() == [_REVISION]
    assert f"Précédente : {_PRECEDENTE}" in Path(rev.path).read_text()


def _colonne(c):
    return c.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'apollo_phone_reveals' AND column_name = 'releve_le'"
    ).fetchone()


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : sans la colonne, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE apollo_phone_reveals DROP COLUMN IF EXISTS releve_le")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_colonne_nait_nullable_et_le_retour_arriere_la_retire(base_d_avant):
    import psycopg
    from alembic import command

    dsn = base_d_avant
    with psycopg.connect(dsn) as c:
        assert _colonne(c) is None
    command.upgrade(_alembic(), _REVISION)
    with psycopg.connect(dsn) as c:
        assert _colonne(c) == ("YES", None)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # rejouée : rien ne bouge
    command.downgrade(_alembic(), _PRECEDENTE)
    with psycopg.connect(dsn) as c:
        assert _colonne(c) is None
    command.upgrade(_alembic(), _REVISION)
