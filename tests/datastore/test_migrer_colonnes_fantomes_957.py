"""L'outil de migration des colonnes fantômes traite les trois populations
différemment (oto-backend#957) — jamais la même façon, sous peine de détruire la
population 1 (la SEULE provenance existante d'une ligne).

Contre un VRAI PostgreSQL : le classement dépend de l'opérateur JSONB `?`/`->>`, pas
d'une supposition Python."""
from __future__ import annotations

import json
import pathlib
import sys

import psycopg
import pytest

# `tests/datastore/` n'a pas de `__init__.py` : pytest insère ce dossier dans
# `sys.path`, pas la racine du dépôt — même patron que
# `test_sentinelles_dans_les_listes.py::_descriptions_servies`.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from scripts.migrer_colonnes_fantomes import main as migrer  # noqa: E402


@pytest.fixture()
def pg(pg_module_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_module_dsn)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS datastore_rows")
        c.execute("CREATE TABLE datastore_rows ("
                  " ns_id INT, row_id TEXT, data JSONB,"
                  " created_at TIMESTAMPTZ DEFAULT now(),"
                  " updated_at TIMESTAMPTZ DEFAULT now())")
        yield c
        c.execute("DROP TABLE IF EXISTS datastore_rows")


def _ins(c, ns, rid, data):
    c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s,%s,%s::jsonb)",
              (ns, rid, json.dumps(data)))


def _data(c, ns, rid):
    return c.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                      (ns, rid)).fetchone()[0]


def test_dry_run_ne_touche_a_rien(pg):
    _ins(pg, 1, "a", {"effectif": 42, "effectif_comment": "registre 2023"})
    migrer(1, "effectif", "comment", apply=False, purger_vides=False)
    assert _data(pg, 1, "a") == {"effectif": 42, "effectif_comment": "registre 2023"}


def test_population_1_est_migree_couche_vide_fantome_pleine(pg):
    _ins(pg, 1, "a", {"effectif": 42, "effectif_comment": "registre — tranche 01"})
    n = migrer(1, "effectif", "comment", apply=True, purger_vides=False)
    assert n == 0
    d = _data(pg, 1, "a")
    assert d == {"effectif": {"valeur": 42, "comment": "registre — tranche 01"}}
    assert "effectif_comment" not in d


def test_population_1_avec_socle_deja_a_couches_garde_les_autres_couches(pg):
    _ins(pg, 1, "a", {"effectif": {"valeur": 42, "origine": "DUPONT"},
                      "effectif_comment": "registre"})
    migrer(1, "effectif", "comment", apply=True, purger_vides=False)
    d = _data(pg, 1, "a")
    assert d["effectif"] == {"valeur": 42, "origine": "DUPONT", "comment": "registre"}


def test_population_2_nest_jamais_touchee(pg):
    """Les deux valeurs diffèrent : ni migrée, ni écrasée — le script la laisse
    intacte et se contente de la signaler."""
    _ins(pg, 1, "a", {"effectif": {"valeur": 42, "comment": "INSEE"},
                      "effectif_comment": "registre — valeur différente"})
    migrer(1, "effectif", "comment", apply=True, purger_vides=True)
    d = _data(pg, 1, "a")
    assert d == {"effectif": {"valeur": 42, "comment": "INSEE"},
                 "effectif_comment": "registre — valeur différente"}


def test_population_3_fantome_vide_nest_pas_effacee_sans_purger_vides(pg):
    _ins(pg, 1, "a", {"effectif": 42, "effectif_comment": ""})
    migrer(1, "effectif", "comment", apply=True, purger_vides=False)
    d = _data(pg, 1, "a")
    assert "effectif_comment" in d


def test_population_3_fantome_vide_est_effacee_avec_purger_vides(pg):
    _ins(pg, 1, "a", {"effectif": 42, "effectif_comment": ""})
    migrer(1, "effectif", "comment", apply=True, purger_vides=True)
    d = _data(pg, 1, "a")
    assert "effectif_comment" not in d
    assert d["effectif"] == 42


def test_les_trois_populations_sur_le_meme_tableau_recoivent_le_bon_geste(pg):
    _ins(pg, 1, "migrer", {"effectif": 10, "effectif_comment": "registre"})
    _ins(pg, 1, "arbitrer", {"effectif": {"valeur": 20, "comment": "INSEE"},
                             "effectif_comment": "autre source"})
    _ins(pg, 1, "vider", {"effectif": 30, "effectif_comment": ""})
    migrer(1, "effectif", "comment", apply=True, purger_vides=True)

    migre = _data(pg, 1, "migrer")
    assert migre == {"effectif": {"valeur": 10, "comment": "registre"}}

    arbitrer = _data(pg, 1, "arbitrer")
    assert arbitrer == {"effectif": {"valeur": 20, "comment": "INSEE"},
                         "effectif_comment": "autre source"}

    vider = _data(pg, 1, "vider")
    assert vider == {"effectif": 30}


def test_idempotent_un_second_passage_ne_trouve_plus_rien(pg):
    _ins(pg, 1, "a", {"effectif": 10, "effectif_comment": "registre"})
    migrer(1, "effectif", "comment", apply=True, purger_vides=True)
    n_avant = migrer(1, "effectif", "comment", apply=True, purger_vides=True)
    assert n_avant == 0


def test_couche_inconnue_est_refusee(pg):
    rc = migrer(1, "effectif", "pas_une_couche", apply=False, purger_vides=False)
    assert rc == 1


def test_scope_par_tableau_ns_id_ne_touche_pas_un_autre_tableau(pg):
    _ins(pg, 1, "a", {"effectif": 10, "effectif_comment": "registre"})
    _ins(pg, 2, "a", {"effectif": 10, "effectif_comment": "registre"})
    migrer(1, "effectif", "comment", apply=True, purger_vides=True)
    assert _data(pg, 1, "a") == {"effectif": {"valeur": 10, "comment": "registre"}}
    assert _data(pg, 2, "a") == {"effectif": 10, "effectif_comment": "registre"}
