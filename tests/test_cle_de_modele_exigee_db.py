"""Le réglage « clé de modèle exigée » EN BASE : il se relit, et l'org l'emporte.

Une doublure ne prouve ni la table, ni la clé primaire, ni la précédence réelle entre
les deux portées. Patron de base éphémère repris de `test_runner_workers_db.py`.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_cle_exigee_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        if avant_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = avant_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def test_rien_de_pose_rien_d_exige(live):
    from oto_mcp.capabilities import _cle_exigee as CE
    from oto_mcp.db import connector_settings as store
    assert store.get_connector_setting("platform", "platform", "anthropic",
                                       CE.CLE_REGLAGE) is None
    assert CE.cle_exigee(7001, "anthropic") is False


def test_la_plateforme_exige_et_l_org_exemptee_ne_l_est_pas(live):
    from oto_mcp.capabilities import _cle_exigee as CE
    from oto_mcp.db import connector_settings as store
    store.set_connector_setting("platform", "platform", "anthropic", CE.CLE_REGLAGE, "true")
    store.set_connector_setting("org", "7002", "anthropic", CE.CLE_REGLAGE, "false")

    assert CE.cle_exigee(7001, "anthropic") is True, "la plateforme vaut pour tous"
    assert CE.cle_exigee(7002, "anthropic") is False, "l'org l'emporte"
    assert CE.cle_exigee(7001, "mistral") is False, "par fournisseur"


def test_une_org_sans_depot_est_listee_manquante(live):
    from oto_mcp.capabilities import _cle_exigee as CE
    assert CE.manquantes(7001) == ["anthropic"]
    assert CE.manquantes(7002) == [], "exemptée, rien ne manque"
