"""La révision `20261113_2300_cn_models_opt_in` rend la Chine OPT-IN : `orgs.allow_cn_models`
passe à DEFAULT FALSE et chaque org à FALSE.

Une base d'AVANT a la colonne née TRUE (`20261112_1900_allow_cn_models`) : la révision
doit y poser le défaut FALSE, remettre chaque org à FALSE — aucune ne pouvait encore faire
tourner ces modèles —, et le retour arrière ne rend que le défaut TRUE. Joué sur une vraie
base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261113_1800_usage_org"
_REVISION = "20261113_2300_cn_models_opt_in"


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


def _defaut(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'orgs' AND column_name = 'allow_cn_models'").fetchone()[0]


def _valeurs(dsn, ids):
    import psycopg
    with psycopg.connect(dsn) as c:
        return [r[0] for r in c.execute(
            "SELECT allow_cn_models FROM orgs WHERE id = ANY(%s) ORDER BY id", (list(ids),))]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : colonne au défaut TRUE, une org à TRUE
    et une qu'un admin avait écartée, estampillée à la révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE orgs ALTER COLUMN allow_cn_models SET DEFAULT TRUE")
        permise = c.execute("INSERT INTO orgs (name) VALUES ('org permise') RETURNING id"
                            ).fetchone()[0]
        ecartee = c.execute("INSERT INTO orgs (name, allow_cn_models) "
                            "VALUES ('org écartée', FALSE) RETURNING id").fetchone()[0]
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn, (permise, ecartee)


def test_la_chine_devient_opt_in_et_le_retour_arriere_rend_le_defaut(base_d_avant):
    from alembic import command

    dsn, ids = base_d_avant
    assert _defaut(dsn) == "true" and _valeurs(dsn, ids) == [True, False]
    command.upgrade(_alembic(), _REVISION)
    assert _defaut(dsn) == "false"
    assert _valeurs(dsn, ids) == [False, False], "chaque org à FALSE"
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _defaut(dsn) == "true"
    assert _valeurs(dsn, ids) == [False, False], "le retour arrière ne rend que le défaut"
    command.upgrade(_alembic(), _REVISION)
    assert _defaut(dsn) == "false"


def test_une_base_neuve_nait_a_FALSE(live):
    """Le DDL du démarrage et de `CREATE TABLE orgs` disent FALSE, comme la révision."""
    from oto_mcp.db import regions_de_modeles
    from oto_mcp.db.schema import orgs
    assert "DEFAULT FALSE" in regions_de_modeles.DDL_COLONNE
    assert "allow_cn_models BOOLEAN NOT NULL DEFAULT FALSE" in orgs.ORGS
