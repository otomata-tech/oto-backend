"""La révision `20261105_1700_orgs_auto_created` pose `orgs.auto_created` et marque les
orgs perso d'avant.

Une colonne NEUVE, `BOOLEAN NOT NULL DEFAULT FALSE`, que le démarrage pose aussi (même
DDL) : la révision doit la poser sur une base d'AVANT, y marquer les orgs perso
(`personal_of` renseigné) et elles seules, ne rien faire de plus sur une base que le
démarrage a déjà servie, et la retirer au retour arrière. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261105_0900_regles_de_comptes"
_REVISION = "20261105_1700_orgs_auto_created"


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
            "WHERE table_name = 'orgs' AND column_name = 'auto_created'"
        ).fetchall()


def _marquees(dsn, ids):
    import psycopg
    with psycopg.connect(dsn) as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT id, auto_created FROM orgs WHERE id = ANY(%s)", (list(ids),))}


_POSEE = [("boolean", "NO", "false")]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : une org perso et une org choisie,
    colonne retirée, estampillée à la révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        perso = c.execute("INSERT INTO orgs (name, personal_of) VALUES ('perso', "
                          "'u-mig-auto') RETURNING id").fetchone()[0]
        choisie = c.execute("INSERT INTO orgs (name) VALUES ('choisie') RETURNING id"
                            ).fetchone()[0]
        c.execute("ALTER TABLE orgs DROP COLUMN auto_created")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn, perso, choisie


def test_la_revision_pose_la_colonne_marque_les_perso_et_le_retour_arriere_la_retire(
        base_d_avant):
    from alembic import command

    dsn, perso, choisie = base_d_avant
    assert _colonne(dsn) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonne(dsn) == _POSEE
    assert _marquees(dsn, (perso, choisie)) == {perso: True, choisie: False}
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _colonne(dsn) == []
    command.upgrade(_alembic(), _REVISION)
    assert _colonne(dsn) == _POSEE
    assert _marquees(dsn, (perso, choisie)) == {perso: True, choisie: False}


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien_de_plus(
        base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser la colonne avant ; le
    marquage rejoué ne touche que les orgs perso, déjà marquées."""
    from alembic import command

    dsn, perso, choisie = base_d_avant
    command.upgrade(_alembic(), _REVISION)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # IF NOT EXISTS, marquage idempotent
    assert _colonne(dsn) == _POSEE
    assert _marquees(dsn, (perso, choisie)) == {perso: True, choisie: False}
