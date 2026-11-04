"""La révision `20261105_0900_regles_de_comptes` pose les règles de comptes et reprend les prêts.

Joué sur une vraie base ramenée à l'état d'avant (tables et fonctions retirées, sans
sous-instance) : la révision pose les tables, les déclencheurs, une sous-instance par
compte Unipile et les règles reprises ; le retour arrière retire les tables et archive
les sous-instances ; rejouée, elle redonne le même état.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261104_1200_runner_jobs_finis"
_REVISION = "20261105_0900_regles_de_comptes"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _un(dsn, sql, params=()):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, params).fetchall()


def _tables(dsn):
    return [r[0] for r in _un(dsn, "SELECT relname FROM pg_class WHERE relname = ANY(%s) "
                                   "AND relkind = 'r' ORDER BY 1",
                              (["connector_account_rules",
                                "connector_account_share_events"],))]


def _sous_instances(dsn):
    return _un(dsn, "SELECT owner_id, account FROM connector_instances WHERE "
                    "connector = 'unipile' AND starts_with(account, 'canal:') "
                    "AND revoked_at IS NULL ORDER BY 1, 2")


def _regles(dsn):
    return _un(dsn, "SELECT instance_id, subject_user, subject_group, subject_org, level, "
                    "services, origine FROM connector_account_rules ORDER BY 1, 2, 3, 4")


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, des partages d'avant, puis ramenée à l'état d'avant."""
    import psycopg
    from alembic import command
    from oto_mcp import db, org_store

    org = org_store.create_org("org_migr")
    for sub in ("proprio", "pair"):
        db.upsert_user(sub)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        for sub in ("proprio", "pair"):
            c.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s)", (org, sub))
        c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                  "secret_enc, share_side) VALUES ('member', %s, 'zoho', 'x', %s)",
                  (f"{org}:proprio", json.dumps(["user:pair"])))
        c.execute("INSERT INTO connector_instances (connector, owner_type, owner_id) "
                  "VALUES ('zoho', 'member', %s), ('attio', 'org', %s)",
                  (f"{org}:proprio", str(org)))
    db.set_unipile_account("proprio", "ACC", None, org_id=org, provider="LINKEDIN")
    db.set_account_grant("proprio", "LINKEDIN", "ACC", "pair", granted_by="proprio")
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE connector_account_share_events")
        c.execute("DROP TABLE connector_account_rules")
        c.execute("DROP FUNCTION connector_account_share_journal()")
        c.execute("DROP FUNCTION connector_account_rules_owner_guard()")
        c.execute("DELETE FROM connector_instances WHERE starts_with(account, 'canal:')")
    command.stamp(_alembic(), _PRECEDENTE)
    return {"dsn": pg_module_dsn, "org": org}


def test_la_revision_pose_reprend_et_le_retour_arriere_retire(base_d_avant):
    from alembic import command
    dsn, org = base_d_avant["dsn"], base_d_avant["org"]
    assert _tables(dsn) == [] and _sous_instances(dsn) == []
    command.upgrade(_alembic(), _REVISION)
    assert _tables(dsn) == ["connector_account_rules", "connector_account_share_events"]
    assert _sous_instances(dsn) == [(f"{org}:proprio", "canal:LINKEDIN")]
    regles = _regles(dsn)
    assert {(r[1], r[2], r[3], r[4], r[6]) for r in regles} == {
        ("pair", None, False, "sensitive", "reprise"),   # prêt du coffre
        (None, None, True, "sensitive", "reprise"),      # compte d'org (a)
    } and len(regles) == 3                               # + le prêt Unipile au pair
    assert len(_un(dsn, "SELECT 1 FROM connector_account_share_events")) == 3
    assert {r[0] for r in _un(dsn, "SELECT tgname FROM pg_trigger WHERE tgrelid = "
                                   "'connector_account_rules'::regclass "
                                   "AND NOT tgisinternal")} == {
        "connector_account_rules_share_journal", "connector_account_rules_owner_guard"}

    command.downgrade(_alembic(), _PRECEDENTE)
    assert _tables(dsn) == [] and _sous_instances(dsn) == []

    command.upgrade(_alembic(), _REVISION)
    assert len(_regles(dsn)) == 3 and len(_sous_instances(dsn)) == 1
