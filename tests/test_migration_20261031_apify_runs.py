"""La révision `20261031_1000_apify_runs` pose la table, et le stockage tient sa promesse.

Joué sur une vraie base : la révision pose la table sur une base d'AVANT, et le premier
chiffre d'un run reste, comme celui que garde le facturant.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261031_0040_agent_preset"
_REVISION = "20261031_1000_apify_runs"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _existe(dsn):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT to_regclass('apify_runs') AS t").fetchone()[0] is not None


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from alembic import command
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE apify_runs")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_et_le_retour_arriere_retire(base_d_avant):
    from alembic import command
    assert not _existe(base_d_avant)
    command.upgrade(_alembic(), _REVISION)
    assert _existe(base_d_avant)
    command.downgrade(_alembic(), _PRECEDENTE)
    assert not _existe(base_d_avant)
    command.upgrade(_alembic(), _REVISION)


def test_le_cycle_d_un_run(base_d_avant):
    from oto_mcp.db import apify_runs as t
    t.noter("r1", org_id=7, sub="s", outil="apify_run", actor_id="a/b", run_id=None,
            plafond_usd=1.0)
    t.noter("r1", org_id=8, sub="x", outil="apify_run", actor_id="a/b", run_id=None,
            plafond_usd=2.0)
    assert t.lire("r1")["org_id"] == 7
    assert t.lire("absent") is None
    assert [r["apify_run_id"] for r in t.a_relever()] == ["r1"]
    assert t.bilan_depuis("2000-01-01")["en_attente"] == 1

    t.marquer("r1", statut="SUCCEEDED", cout_usd=5.08)
    t.marquer("r1", statut="SUCCEEDED", cout_usd=9.0)
    assert float(t.lire("r1")["cout_usd"]) == 5.08
    assert t.a_relever() == []
    bilan = t.bilan_depuis("2000-01-01")
    assert bilan["releve_usd"] == 5.08 and bilan["en_attente"] == 0
    assert t.purger() == 0
    assert t.purger(jours=0) == 1


def test_un_run_releve_se_lit_dans_la_lentille_de_l_org_qui_l_a_lance(base_d_avant):
    """Le point de tout : la ligne du relevé est celle que le facturant lit, sous l'org
    qui a lancé, avec le run pour job — et rien sous une autre org."""
    from oto_mcp import apify_releve
    from oto_mcp.db import usage
    ligne = {"apify_run_id": "r2", "sub": "s", "org_id": 7301, "run_id": None}
    apify_releve._tracer(ligne, 5.08865)

    appels = usage.list_billable_calls_for_org(7301, "apify_run_status")["calls"]
    assert [(a["job_id"], a["quantity"], a["key_mode"]) for a in appels] == [
        ("r2", 5088650, "platform")]
    assert usage.list_billable_calls_for_org(7302, "apify_run_status")["calls"] == []
