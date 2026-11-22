"""La révision `20261122_1200_repli_quand` pose QUAND une org se replie
(`org_model_subscription_modes.repli_quand`, `_by`, `_at`).

Sur une base d'AVANT (colonnes absentes, une ligne de réglage existante) : les colonnes
naissent, la ligne existante lit `plafond` (rien ne change pour elle) ; la colonne
refuse une valeur hors contrat ; rejouée, elle ne fait rien ; le retour arrière retire
les colonnes et garde la ligne. Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261122_1100_repli_plateforme"
_REVISION = "20261122_1200_repli_quand"
_COLONNES = ("repli_quand", "repli_quand_by", "repli_quand_at")


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


def _colonnes(c):
    return {r[0] for r in c.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'org_model_subscription_modes'").fetchall()}


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : les trois colonnes retirées, une
    ligne de réglage d'org existante, estampillée à la révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        for col in _COLONNES:
            c.execute(f"ALTER TABLE org_model_subscription_modes DROP COLUMN {col}")
        oid = c.execute("INSERT INTO orgs (name) VALUES ('org d''avant') RETURNING id"
                        ).fetchone()[0]
        c.execute("INSERT INTO org_model_subscription_modes (org_id, famille, mode, "
                  "repli_api) VALUES (%s, 'claude_subscription', 'pool', TRUE)", (oid,))
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn, oid


def test_les_colonnes_naissent_au_plafond_et_le_retour_arriere_les_retire(base_d_avant):
    import psycopg
    from alembic import command

    dsn, oid = base_d_avant
    with psycopg.connect(dsn) as c:
        assert not set(_COLONNES) & _colonnes(c)
    command.upgrade(_alembic(), _REVISION)
    with psycopg.connect(dsn) as c:
        assert set(_COLONNES) <= _colonnes(c)
        ligne = c.execute(
            "SELECT mode, repli_api, repli_quand, repli_quand_by, repli_quand_at "
            "FROM org_model_subscription_modes WHERE org_id = %s", (oid,)).fetchone()
        assert ligne == ("pool", True, "plafond", None, None), "rien ne change pour elle"
    with psycopg.connect(dsn) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE org_model_subscription_modes SET repli_quand = 'toujours' "
                      "WHERE org_id = %s", (oid,))
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)   # rejouée : rien ne bouge
    command.downgrade(_alembic(), _PRECEDENTE)
    with psycopg.connect(dsn) as c:
        assert not set(_COLONNES) & _colonnes(c)
        assert c.execute("SELECT repli_api FROM org_model_subscription_modes "
                         "WHERE org_id = %s", (oid,)).fetchone() == (True,)
    command.upgrade(_alembic(), _REVISION)
