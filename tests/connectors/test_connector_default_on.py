"""`seed_default_on` — une instance qui ouvre d'office les nouveaux connecteurs.

Exercé contre un vrai PostgreSQL (fixture `pg_module_dsn`) : ce qui compte ici est
la PK `(scope_type, scope_id, connector)` et le `ON CONFLICT DO NOTHING` — un OFF
explicite doit survivre au boot, et c'est la base qui le garantit, pas le code.
"""
from __future__ import annotations

import pytest

from oto_mcp import interrupteurs, providers
from oto_mcp.connectors import activation as act

FLAG = "OTO_CONNECTEURS_ACTIFS_PAR_DEFAUT"


@pytest.fixture()
def conn(pg_module_dsn):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS connector_availability")
        act.init_schema(c)
        yield c


def _platform(conn) -> dict:
    return {r["connector"]: r["enabled"] for r in conn.execute(
        "SELECT connector, enabled FROM connector_availability WHERE scope_type = 'platform'")}


def test_sans_declaration_rien_ne_bouge(conn, monkeypatch):
    """L'instance qui ne déclare rien garde le deny-by-default."""
    monkeypatch.delenv(FLAG, raising=False)
    assert act.seed_default_on(conn) == 0
    assert _platform(conn) == {}


def test_ouvre_ce_qui_na_aucune_ligne_et_respecte_un_off(conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    nom_off, nom_on = list(providers.REGISTRY)[:2]
    conn.execute(
        "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled) "
        "VALUES ('platform', '', %s, FALSE), ('platform', '', %s, TRUE), "
        "       ('org', '7', %s, FALSE)",
        (nom_off, nom_on, nom_on))
    ouverts = act.seed_default_on(conn)
    vue = _platform(conn)
    assert ouverts == len(providers.REGISTRY) - 2
    assert vue[nom_off] is False                 # le OFF explicite reste OFF
    assert all(vue[n] for n in providers.REGISTRY if n != nom_off)
    # l'override d'org n'est pas touché
    assert conn.execute(
        "SELECT enabled FROM connector_availability WHERE scope_type = 'org' "
        "AND scope_id = '7' AND connector = %s", (nom_on,)).fetchone()["enabled"] is False


def test_rejouable(conn, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    act.seed_default_on(conn)
    assert act.seed_default_on(conn) == 0


def test_une_valeur_illisible_leve(conn, monkeypatch):
    monkeypatch.setenv(FLAG, "peut-etre")
    with pytest.raises(interrupteurs.InterrupteurIllisible):
        act.seed_default_on(conn)
