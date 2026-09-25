"""Le fan-out 1→N du split google (2026-09-26), contre un vrai PostgreSQL.

Même mécanique que `test_unipile_split_fanout.py`, et la même raison d'exister : les
tables de gouvernance ne connaissent que `google`, et deux d'entre elles penchent du
mauvais côté pour un nom absent (disponibilité ⟹ OFF, sélection ⟹ masqué). Ce que ce
banc ajoute : le split google a SA sentinelle — la base de prod porte déjà celle
d'unipile, et la lire pour google ferait passer le déménagement pour fait.
"""
from __future__ import annotations

import pytest

from oto_mcp.connectors import activation as act
from oto_mcp.connectors import selection as sel

SERVICES = sel.GOOGLE_SERVICES


@pytest.fixture()
def conn(pg_module_dsn):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_module_dsn, row_factory=dict_row, autocommit=True) as c:
        for t in ("user_selected_connectors", "connector_selection_seeded",
                  "connector_availability"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        sel.init_schema(c)
        act.init_schema(c)
        yield c


def _selection(conn) -> dict:
    return {(r["sub"], r["org_id"], r["connector"]): r["state"]
            for r in conn.execute(
                "SELECT sub, org_id, connector, state FROM user_selected_connectors")}


def test_la_selection_du_compte_se_propage_aux_six_services_et_le_compte_survit(conn):
    conn.execute("INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                 "VALUES ('u1', 1, 'google', 'active')")
    assert sel.fanout_selection(conn, "google", SERVICES) == 6
    vue = _selection(conn)
    assert all(vue[("u1", 1, s)] == "active" for s in SERVICES)
    assert vue[("u1", 1, "google")] == "active"


def test_lexposition_suit_aux_trois_scopes(conn):
    conn.execute(
        "INSERT INTO connector_availability (scope_type, scope_id, connector, enabled) "
        "VALUES ('platform', '', 'google', TRUE), ('org', '7', 'google', FALSE)")
    assert act.fanout_availability(conn, "google", SERVICES) == 12
    rows = {(r["scope_type"], r["scope_id"], r["connector"]): r["enabled"]
            for r in conn.execute("SELECT scope_type, scope_id, connector, enabled "
                                  "FROM connector_availability")}
    for s in SERVICES:
        assert rows[("platform", "", s)] is True, s
        assert rows[("org", "7", s)] is False, s


def test_la_sentinelle_du_split_google_est_la_sienne(conn):
    """La prod porte déjà la sentinelle d'unipile : elle ne vaut pas pour google."""
    sel.mark_split_fanout(conn)                      # unipile, déjà posée en prod
    assert sel.split_fanout_pending(conn, SERVICES, mark=sel.GOOGLE_SPLIT_MARK) is True
    sel.mark_split_fanout(conn, sel.GOOGLE_SPLIT_MARK)
    assert sel.split_fanout_pending(conn, SERVICES, mark=sel.GOOGLE_SPLIT_MARK) is False
    # Et réciproquement : marquer google ne marque pas unipile.
    conn.execute("DELETE FROM connector_selection_seeded WHERE sub = %s", (sel._SPLIT_MARK,))
    assert sel.split_fanout_pending(conn, ("whatsapp",)) is True


def test_une_base_deja_migree_est_marquee_sans_reecrire(conn):
    """Une base qui porte déjà une sélection sur un service a reçu le déménagement :
    on marque, on n'écrit pas — sinon un service retiré reviendrait au boot."""
    conn.execute("INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                 "VALUES ('u1', 1, 'google', 'active'), ('u1', 1, 'drive', 'active')")
    assert sel.split_fanout_pending(conn, SERVICES, mark=sel.GOOGLE_SPLIT_MARK) is False
    assert sel.split_fanout_pending(conn, SERVICES, mark=sel.GOOGLE_SPLIT_MARK) is False
    assert set(_selection(conn)) == {("u1", 1, "google"), ("u1", 1, "drive")}
