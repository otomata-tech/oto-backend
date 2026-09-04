"""`POST …/rows` refuse un lot enveloppé au lieu d'en faire une ligne (oto#48).

**Le fait, mesuré sur base réelle le 04/09/2026.** Une mission charge par lots de
cent en `{"data": [...]}` sur la route unitaire : quinze 201, quinze lignes, chacune
portant cent lignes imbriquées sous une colonne `data`. Sans schéma, pas un mot.
Avec `strict: true`, la ligne s'écrit quand même, avec un relevé `hors_schema`.
Seul `unknown_fields: "reject"` refusait — sous le nom « colonne inconnue ».

Ce banc tourne sur une **vraie base**, par la **chaîne servie** (adaptateur REST),
et lit `datastore_rows` : la seule question qui vaille est « qu'y a-t-il en base
après l'appel ? ». Ce qu'il fige :

- les deux corps « lot » (`rows`, `data`) sont refusés `400 batch_body`, rien d'écrit ;
- la liste à la racine reste refusée (garde générique de l'adaptateur, inchangée) ;
- une ligne normale, et une colonne libre à liste de SCALAIRES, s'écrivent comme avant ;
- une colonne DÉCLARÉE `list` à clé unique reste écrivable (le schéma dit l'intention) ;
- sur `strict` et sur `unknown_fields: "reject"`, le lot est refusé et la ligne
  normale passe — le schéma continue de juger les colonnes, la garde ne juge que
  la forme ;
- le PATCH n'est pas gardé : réécrire une sous-table d'une ligne reste un geste juste.
"""
from __future__ import annotations

import uuid

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.capabilities.datastore.lot import forme_de_lot


# ── la forme, sans base ─────────────────────────────────────────────────────

@pytest.mark.parametrize("row,attendu", [
    ({"rows": [{"a": 1}, {"a": 2}]}, ("rows", 2)),
    ({"data": [{"a": 1}]}, ("data", 1)),
    ({"tags": ["x", "y"]}, None),              # liste de scalaires : une colonne
    ({"rows": []}, None),                      # liste vide : rien à prendre pour un lot
    ({"siren": "1", "contacts": [{"n": 1}]}, None),   # deux clés : une ligne
    ({"rows": [{"a": 1}, "b"]}, None),         # mélange : pas un lot
    ({}, None),
    ([{"a": 1}], None),                        # la liste à la racine est jugée AVANT
])
def test_la_forme_d_un_lot(row, attendu):
    assert forme_de_lot(row) == attendu


# ── la chaîne servie, sur base réelle ───────────────────────────────────────

@pytest.fixture(scope="module")
def live(pg_dsn):
    import os
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_lot48_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


SUB = "sub-test"
LOT = [{"siren": "1", "nom": "A"}, {"siren": "2", "nom": "B"}]
LIGNE = {"siren": "3", "nom": "C"}
STRICT = {"strict": True, "fields": [{"key": "siren", "type": "text"},
                                     {"key": "nom", "type": "text"}]}
REJECT = {**STRICT, "unknown_fields": "reject"}
SOUS_TABLE = {"fields": [{"key": "contacts", "type": "list",
                         "of": {"type": "object",
                                "fields": [{"key": "nom", "type": "text"}]}}]}


@pytest.fixture(autouse=True)
def _autz(monkeypatch):
    stub_authz(monkeypatch, org_id=None)


def _table(schema=None):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", SUB, ns)
    if schema is not None:
        make_store(SUB).set_schema(ns, schema)
    return ns, ns_id


def _base(ns_id: int) -> list[dict]:
    """Ce que porte LA BASE — jamais ce que l'appel a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return [dict(r["data"]) for r in conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s", (ns_id,)).fetchall()]


def _post(ns, corps):
    return call("me.datastore.append_row", path_params={"namespace": ns},
                body=corps, sub=SUB)


@pytest.mark.parametrize("cle", ["rows", "data"])
def test_un_lot_enveloppe_est_refuse_nomme_et_rien_n_est_ecrit(live, cle):
    ns, ns_id = _table()
    status, rep = _post(ns, {cle: LOT})
    assert (status, rep["error"]) == (400, "batch_body")
    assert rep["details"] == {"key": cle, "count": 2}
    # Le message dit la forme attendue ET où va le lot — il sera cité tel quel.
    assert "UNE ligne" in rep["detail"]
    assert "data_write" in rep["detail"] and "oto_upload_url" in rep["detail"]
    assert _base(ns_id) == []


def test_la_liste_a_la_racine_reste_refusee(live):
    ns, ns_id = _table()
    status, rep = _post(ns, LOT)
    assert status == 400 and rep["error"] == "invalid_body"
    assert _base(ns_id) == []


def test_une_ligne_normale_s_ecrit(live):
    ns, ns_id = _table()
    status, rep = _post(ns, LIGNE)
    assert status == 201 and rep["siren"] == "3"
    assert _base(ns_id) == [LIGNE]


def test_une_colonne_libre_a_liste_de_scalaires_s_ecrit(live):
    """Le schéma libre accepte une liste de scalaires aujourd'hui ; il continue."""
    ns, ns_id = _table()
    status, rep = _post(ns, {"siren": "4", "tags": ["x", "y"]})
    assert status == 201 and rep["tags"] == ["x", "y"]
    assert _base(ns_id) == [{"siren": "4", "tags": ["x", "y"]}]


def test_une_colonne_declaree_list_a_cle_unique_reste_ecrivable(live):
    """La déclaration l'emporte sur la forme : un sous-tableau déclaré est une ligne."""
    ns, ns_id = _table(SOUS_TABLE)
    corps = {"contacts": [{"nom": "A"}, {"nom": "B"}]}
    status, rep = _post(ns, corps)
    assert status == 201 and rep["contacts"] == corps["contacts"]
    assert _base(ns_id) == [corps]


@pytest.mark.parametrize("schema", [STRICT, REJECT], ids=["strict", "reject"])
def test_sur_schema_strict_le_lot_est_refuse_et_la_ligne_passe(live, schema):
    ns, ns_id = _table(schema)
    status, rep = _post(ns, {"rows": LOT})
    assert (status, rep["error"]) == (400, "batch_body")
    assert _base(ns_id) == []
    status, rep = _post(ns, LIGNE)
    assert status == 201 and "hors_schema" not in rep
    assert _base(ns_id) == [LIGNE]


def test_le_patch_n_est_pas_garde(live):
    """Réécrire la sous-table d'une ligne existante : un geste juste, non gardé."""
    ns, ns_id = _table()
    rid = _post(ns, LIGNE)[1]["_id"]
    status, rep = call("me.datastore.update_row",
                       path_params={"namespace": ns, "row_id": rid},
                       body={"contacts": [{"nom": "A"}]}, sub=SUB)
    assert status == 200 and rep["contacts"] == [{"nom": "A"}]
    assert _base(ns_id) == [{**LIGNE, "contacts": [{"nom": "A"}]}]
