"""Le lot sur la face REST : `POST /api/datastores/{datastore}/rows/batch` (oto#151).

**Le fait.** Un client REST pur a rechargé ~8 900 lignes. Il a posté `{"rows": […],
"key": …}` sur la route d'UNE ligne — le corps que l'écriture agent lui suggérait —,
reçu `business_key_required` pour une clé qu'il venait d'envoyer, et fini en 8 907
`PATCH` ligne à ligne. Aucune route de lot n'existait : sa conclusion était juste.

Ce banc tourne sur la table de routes RÉELLE (`make_routes` + adaptateur) et sur le
tool MCP monté, contre un vrai PostgreSQL, et lit `datastore_rows`. Ce qu'il fige :

- le lot upsert sur la clé métier déclarée, ou sur `key` passé à l'appel ;
- `donnees_d_origine=true` pose l'origine, et un ré-import ne la touche pas ;
- un mot refusé (`@keep` après sa date) refuse le lot ENTIER : rien d'écrit ;
- une ligne refusée par le schéma est NOMMÉE (rang, reste écrit avant l'arrêt) ;
- les deux faces rendent la même enveloppe et les mêmes notices ;
- un champ inconnu du corps est refusé, jamais ignoré.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@lot.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


SUB = "usr_lot151"
SCHEMA = {"key": "siren", "fields": [{"key": "siren", "type": "text"},
                                     {"key": "nom", "type": "text"},
                                     {"key": "effectif", "type": "number"}]}


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_lot151_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp import db
        db.upsert_user(SUB, email=f"{SUB}@lot.invalid", name=SUB)
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


def _table(schema=None):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    nom = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, nom)
    ns = str(ns_id)
    if schema is not None:
        make_store(SUB).set_schema(ns, schema)
    return ns, ns_id, nom


def _base(ns_id: int) -> dict:
    """Ce que porte LA BASE, par siren — jamais ce que l'appel a bien voulu rendre."""
    from oto_mcp.datastore import schema as dsv2
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = [dict(r["data"]) for r in conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s", (ns_id,)).fetchall()]
    return {dsv2.unwrap(r.get("siren")): r for r in rows}


def _lot(client, ns, corps, query: str = ""):
    return client.post(f"/api/datastores/{ns}/rows/batch{query}", headers=_h(), json=corps)


@pytest.fixture
def data_write(live, monkeypatch):
    """Le tool MCP `data_write` monté, tel qu'un agent l'appelle."""
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return asyncio.run(mcp.get_tool("data_write")).fn


# ── le lot, par la clé métier ─────────────────────────────────────────────────

def test_le_lot_upsert_sur_la_cle_metier_declaree(client):
    ns, ns_id, nom = _table(SCHEMA)
    r1 = _lot(client, ns, {"rows": [{"siren": "1", "nom": "A"}, {"siren": "2", "nom": "B"}]})
    assert r1.status_code == 200, r1.text
    rep = r1.json()
    assert (rep["inserted"], rep["updated"], rep["count"]) == (2, 0, 2)
    assert rep["key"] == "siren" and len(rep["ids"]) == 2
    assert rep["ns_id"] == ns_id and rep["datastore"] == nom
    # Ré-envoi avec `upsert` : la ligne retrouvée par sa clé est FUSIONNÉE, la neuve
    # est créée (sans `upsert`, un ajout sur une clé en place est refusé, oto#141).
    r2 = _lot(client, ns, {"rows": [{"siren": "2", "nom": "B bis"}, {"siren": "3", "nom": "C"}],
                           "upsert": True})
    assert r2.status_code == 200, r2.text
    assert (r2.json()["inserted"], r2.json()["updated"]) == (1, 1)
    base = _base(ns_id)
    assert sorted(base) == ["1", "2", "3"]
    assert base["2"]["nom"] == "B bis"


def test_le_lot_dedoublonne_sur_la_cle_passee_a_l_appel(client):
    """Sans clé déclarée, `key` du corps dédoublonne — le cas exact du rapport."""
    ns, ns_id, _nom = _table({"fields": SCHEMA["fields"]})
    corps = {"rows": [{"siren": "9", "nom": "X"}], "key": "siren"}
    assert _lot(client, ns, corps).json()["inserted"] == 1
    r = _lot(client, ns, {"rows": [{"siren": "9", "nom": "Y"}], "key": "siren"})
    assert (r.json()["inserted"], r.json()["updated"]) == (0, 1)
    base = _base(ns_id)
    assert list(base) == ["9"] and base["9"]["nom"] == "Y"
    # `rows` et `key` n'ont PAS fini en colonnes.
    assert "rows" not in base["9"] and "key" not in base["9"]


# ── l'import ──────────────────────────────────────────────────────────────────

def test_donnees_d_origine_pose_l_origine_et_le_reimport_ne_la_touche_pas(client):
    from oto_mcp.datastore import schema as dsv2
    ns, ns_id, _nom = _table(SCHEMA)
    r1 = _lot(client, ns, {"rows": [{"siren": "1", "nom": {"valeur": "DUPONT",
                                                          "comment": "fichier du 05/08"}}],
                           "donnees_d_origine": True})
    assert r1.status_code == 200, r1.text
    assert any("origin SET" in n and "`nom`" in n for n in r1.json()["notices"])
    cell = _base(ns_id)["1"]["nom"]
    origine = dsv2.layer_value(cell, dsv2.ORIGIN_LAYER)
    assert dsv2.unwrap(origine) == "DUPONT"
    assert dsv2.layer_value(origine, "comment") == "fichier du 05/08"
    # Ré-import, valeur changée : le courant bouge, l'origine reste figée — et c'est dit.
    r2 = _lot(client, ns, {"rows": [{"siren": "1", "nom": "Dupont SAS"}], "upsert": True},
              query="?donnees_d_origine=true")
    assert r2.status_code == 200, r2.text
    assert r2.json()["updated"] == 1
    assert any("origin NOT set" in n and "already set" in n for n in r2.json()["notices"])
    cell = _base(ns_id)["1"]["nom"]
    assert dsv2.unwrap(cell) == "Dupont SAS"
    assert dsv2.unwrap(dsv2.layer_value(cell, dsv2.ORIGIN_LAYER)) == "DUPONT"


# ── rien d'écrit à moitié, et les refus nommés ───────────────────────────────

def test_un_mot_refuse_refuse_le_lot_entier_rien_n_est_ecrit(client):
    """`@keep` sur la DEUXIÈME ligne refuse le lot avant la première."""
    ns, ns_id, _nom = _table(SCHEMA)
    r = _lot(client, ns, {"rows": [{"siren": "1", "nom": "A"},
                                   {"siren": "2", "nom": "@keep"}]})
    assert r.status_code == 400, r.text
    assert "@keep" in r.json()["detail"]
    assert _base(ns_id) == {}


def test_une_ligne_refusee_est_nommee_et_le_refus_dit_ou_reprendre(
        client):
    ns, ns_id, _nom = _table(SCHEMA)
    r = _lot(client, ns, {"rows": [{"siren": "1", "effectif": 3},
                                   {"siren": "2", "effectif": "beaucoup"}]})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "row_invalid"
    assert "row 2/2" in r.json()["detail"] and "resume the batch at row 2" in r.json()["detail"]
    assert sorted(_base(ns_id)) == ["1"]


def test_un_champ_inconnu_du_corps_est_refuse(client):
    ns, ns_id, _nom = _table(SCHEMA)
    r = _lot(client, ns, {"rows": [{"siren": "1"}], "cle": "siren"})
    assert (r.status_code, r.json()["error"]) == (400, "unknown_fields")
    assert _base(ns_id) == {}


# ── les deux faces, un seul geste ────────────────────────────────────────────

def test_les_deux_faces_rendent_la_meme_enveloppe_et_les_memes_notices(client, data_write):
    """`donnees_d_origine` relève ce qu'il a posé : une notice, qui doit être la même
    phrase sur les deux faces."""
    lot = [{"siren": "1", "nom": "A"}, {"siren": "2", "nom": "B"}]
    ns_rest, _, _ = _table(SCHEMA)
    ns_mcp, _, _ = _table(SCHEMA)
    rest = _lot(client, ns_rest, {"rows": lot, "donnees_d_origine": True})
    assert rest.status_code == 200, rest.text
    mcp = data_write(datastore=ns_mcp, rows=lot, donnees_d_origine=True)
    rest = rest.json()
    assert rest["notices"] and rest["notices"] == mcp["notices"]
    for cle in ("inserted", "updated", "count", "key"):
        assert rest[cle] == mcp[cle], cle


# ── §3 : `business_key_required` distingue la clé absente de la clé inconnue ──

FERME = {**SCHEMA, "new_rows": "reject"}


def _refus_mcp(data_write, **kw) -> str:
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as exc:
        data_write(**kw)
    return exc.value.error.message


@pytest.mark.parametrize("ligne, portee", [({"siren": "999", "nom": "X"}, True),
                                          ({"nom": "X"}, False)])
def test_le_refus_de_cle_dit_si_l_ecriture_la_portait_sur_les_deux_faces(
        client, data_write, ligne, portee):
    """Même refus, quatre chemins : REST ligne, REST lot, `data_write(row=)`,
    `data_write(rows=)`. REST porte `details` ; MCP, qui n'a pas d'enveloppe, finit
    son message par la charge à renvoyer. Rien n'est écrit."""
    ns, ns_id, _nom = _table(FERME)
    attendu = {"key": "siren", "cle_portee": portee,
               **({"valeur": "999"} if portee else {}),
               "a_renvoyer": {"siren": "<text>"}}
    ouverture = ("this write carries `siren` = '999'" if portee
                 else "this write does not carry `siren`")
    for r in (client.post(f"/api/datastores/{ns}/rows", headers=_h(), json=ligne),
              _lot(client, ns, {"rows": [ligne]})):
        assert r.status_code == 400, r.text
        corps = r.json()
        assert corps["error"] == "business_key_required"
        assert corps["details"] == attendu
        assert ouverture in corps["detail"]
    for kw in ({"row": ligne}, {"rows": [ligne]}):
        msg = _refus_mcp(data_write, datastore=ns, **kw)
        assert ouverture in msg
        assert msg.endswith('To send back, with the `<…>` templates replaced: '
                            '{"siren": "<text>"}.')
    assert _base(ns_id) == {}
