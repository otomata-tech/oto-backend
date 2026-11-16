"""La révision `20261111_1930_dataforseo_decrit` réécrit les listes d'outils enregistrées
de la bascule de DataForSEO vers sa description (11/11/2026).

Les huit outils écrits à la main deviennent `dataforseo`, dans les listes qui autorisent
comme dans celles qui masquent, sur place et sans doublon ; un travail terminé n'est pas
réécrit. Joué sur une vraie base, estampillée à la révision d'avant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261111_1510_cell_sources"
_REVISION = "20261111_1930_dataforseo_decrit"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def test_chaque_ancien_nom_donne_dataforseo_des_deux_cotes():
    import importlib.util
    chemin = RACINE / "oto_mcp" / "db" / "migrations" / "versions" / f"{_REVISION}.py"
    spec = importlib.util.spec_from_file_location("revision_dataforseo_decrit", chemin)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    autorise, masque = m.correspondances()
    anciens = {"dataforseo_keywords", "dataforseo_domain", "dataforseo_backlinks",
               "dataforseo_account", "dataforseo_ai_visibility", "dataforseo_web_mentions",
               "dataforseo_site_audit", "dataforseo_technologies"}
    assert set(autorise) == set(masque) == anciens
    assert set(autorise.values()) == set(masque.values()) == {("dataforseo",)}


@pytest.fixture(scope="module")
def base_montee(live, pg_module_dsn):
    import psycopg
    from alembic import command

    command.stamp(_alembic(), _PRECEDENTE)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("INSERT INTO projects (id, owner_id, name, mcp_tools) VALUES "
                  "(94001, 'u1', 'p', ARRAY['dataforseo_keywords','serper_search',"
                  "'dataforseo_domain']), "
                  "(94002, 'u1', 'q', ARRAY['gmail'])")
        c.execute("INSERT INTO runner_fleets (id, org_id, sub, label, procedure, tools) "
                  "VALUES (94001, 1, 'u1', 'f', 'p', %s::jsonb)",
                  (json.dumps(["dataforseo_technologies", "fr_get"]),))
        for jid, statut in ((94001, "pending"), (94002, "failed")):
            c.execute("INSERT INTO runner_jobs (id, org_id, kind, status, payload) VALUES "
                      "(%s, 1, 'start', %s, %s::jsonb)",
                      (jid, statut, json.dumps({"tools": ["dataforseo_backlinks", "fr_get"],
                                                "procedure": "p"})))
        c.execute("INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES "
                  "('u1', 0, 'dataforseo_site_audit'), ('u1', 0, 'dataforseo_account')")
        c.execute("INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES "
                  "('u2', 3, 'dataforseo_keywords')")
    command.upgrade(_alembic(), _REVISION)
    return pg_module_dsn


def _lire(dsn, sql, *args):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_les_listes_qui_autorisent_recoivent_dataforseo_sur_place(base_montee):
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 94001") == [
        (["dataforseo", "serper_search"],)]
    assert _lire(base_montee, "SELECT mcp_tools FROM projects WHERE id = 94002") == [
        (["gmail"],)]
    assert _lire(base_montee, "SELECT tools FROM runner_fleets WHERE id = 94001") == [
        (["dataforseo", "fr_get"],)]
    assert _lire(base_montee, "SELECT tool_name FROM user_enabled_tools "
                              "WHERE sub = 'u2'") == [("dataforseo",)]


def test_les_listes_qui_masquent_recoivent_dataforseo_une_fois(base_montee):
    assert _lire(base_montee, "SELECT tool_name FROM user_disabled_tools "
                              "WHERE sub = 'u1'") == [("dataforseo",)]


def test_un_travail_termine_n_est_pas_reecrit(base_montee):
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 94001") \
        == [(["dataforseo", "fr_get"],)]
    assert _lire(base_montee, "SELECT payload->'tools' FROM runner_jobs WHERE id = 94002") \
        == [(["dataforseo_backlinks", "fr_get"],)]
